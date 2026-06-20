"""
Argus Dashboard — FastAPI backend serving the React frontend,
REST API for incidents/stats, SSE for live events, and chat endpoint.
"""

import os
import json
import asyncio
import threading
import sqlite3
import queue as _queue
from collections import deque
from contextlib import contextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from config import DB_PATH
from agent.loop import AgentLoop
from agent.session import SessionStore

app = FastAPI(title="Argus Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Thread-safe event bus

_EVENT_BUS_MAX = 500
_event_bus: deque[dict] = deque(maxlen=_EVENT_BUS_MAX)
_event_bus_lock = threading.Lock()
_event_id_counter = 0


def push_event(event_type: str, message: str) -> None:
    """Called from the K8s watcher thread. Thread-safe."""
    global _event_id_counter
    with _event_bus_lock:
        _event_id_counter += 1
        _event_bus.append({
            "id":      _event_id_counter,
            "type":    event_type,
            "message": message,
            "time":    datetime.now().strftime("%H:%M:%S.%f")[:-3],
        })


def get_events_since(last_id: int) -> list[dict]:
    with _event_bus_lock:
        return [e for e in _event_bus if e["id"] > last_id]


@contextmanager
def get_db():
    """Open a fresh SQLite connection; guarantees close."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@app.get("/api/stats")
async def get_stats():
    with get_db() as db:
        active_incidents = db.execute(
            "SELECT COUNT(*) AS c FROM incidents WHERE status='ACTIVE'"
        ).fetchone()["c"]
        total_findings = db.execute(
            "SELECT COUNT(*) AS c FROM findings"
        ).fetchone()["c"]
        suppressed = db.execute(
            "SELECT COUNT(DISTINCT finding_id) AS c "
            "FROM incident_findings WHERE role='SYMPTOM'"
        ).fetchone()["c"]
    return {
        "active_incidents":    active_incidents,
        "total_findings":      total_findings,
        "suppressed_findings": suppressed,
        "noise_reduction":     round(suppressed / total_findings * 100, 1) if total_findings else 0,
    }


@app.get("/api/incidents")
async def get_incidents():
    with get_db() as db:
        rows = db.execute("""
            SELECT
                i.id, i.title, i.scope, i.resource, i.severity,
                i.status, i.correlation_rule, i.first_seen, i.last_seen,
                i.diagnosis,
                COUNT(DISTINCT if1.finding_id)                                       AS total_findings,
                COUNT(DISTINCT CASE WHEN if1.role='SYMPTOM'    THEN if1.finding_id END) AS suppressed_count,
                COUNT(DISTINCT CASE WHEN if1.role='ROOT_CAUSE' THEN if1.finding_id END) AS root_count
            FROM incidents i
            LEFT JOIN incident_findings if1 ON if1.incident_id = i.id
            WHERE i.status = 'ACTIVE'
            GROUP BY i.id
            ORDER BY i.first_seen DESC
        """).fetchall()

    result = []
    for r in rows:
        row = dict(r)
        if row.get("diagnosis"):
            try:
                row["diagnosis"] = json.loads(row["diagnosis"])
            except Exception:
                pass
        result.append(row)
    return result


@app.get("/api/incidents/{incident_id}")
async def get_incident_detail(incident_id: str):
    with get_db() as db:
        inc = db.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)
        ).fetchone()
        if not inc:
            return {"error": "Not found"}
        findings = db.execute("""
            SELECT f.*, if1.role
            FROM incident_findings if1
            JOIN findings f ON f.id = if1.finding_id
            WHERE if1.incident_id = ?
            ORDER BY
                CASE if1.role WHEN 'ROOT_CAUSE' THEN 0 WHEN 'CONTRIBUTING' THEN 1 ELSE 2 END,
                f.severity DESC, f.last_seen DESC
        """, (incident_id,)).fetchall()

    incident_dict = dict(inc)
    if incident_dict.get("diagnosis"):
        try:
            incident_dict["diagnosis"] = json.loads(incident_dict["diagnosis"])
        except Exception:
            pass
    return {"incident": incident_dict, "findings": [dict(f) for f in findings]}



@app.get("/api/events/recent")
async def get_recent_events():
    """Return the last 100 in-memory events for initial page load."""
    with _event_bus_lock:
        return list(_event_bus)[-100:]


@app.get("/api/stream")
async def event_stream():
    async def generate():
        # Start from the current tail so the client only sees NEW events.
        with _event_bus_lock:
            last_id = _event_id_counter
        while True:
            for ev in get_events_since(last_id):
                last_id = ev["id"]
                yield {"event": "message", "data": json.dumps(ev)}
            await asyncio.sleep(0.3)

    return EventSourceResponse(generate())


@app.post("/api/actions/execute")
async def execute_action(request: Request):
    body = await request.json()
    tool_name = body.get("tool_name")
    parameters = body.get("parameters", {})
    
    from engine.action_executor import ActionExecutor
    executor = ActionExecutor()
    result = executor.execute_approved_action(tool_name, parameters)
    
    if result.get("success", False) is False:
        return {"success": False, "error": result.get("error", "Execution failed")}
        
    return {"success": True, "result": result}

# ── Chat API ─────────────────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """Stream chat responses via SSE. Creates a session on first call."""
    body = await request.json()
    session_id = body.get("session_id")
    message = (body.get("message") or "").strip()

    if not message:
        return {"error": "Empty message"}

    # Validate / create session (short-lived connection on main thread)
    store = SessionStore()
    try:
        if not session_id or not store.session_exists(session_id):
            session_id = store.create_session(title=message[:60])
    finally:
        store.close()

    q: _queue.Queue = _queue.Queue()

    def _run_agent():
        thread_store = SessionStore()
        try:
            agent = AgentLoop(session_id=session_id, session_store=thread_store)
            for event in agent.chat(message):
                q.put(event)
        except Exception as exc:
            q.put({"type": "error", "content": str(exc)})
        finally:
            thread_store.close()
            q.put(None)  # sentinel

    threading.Thread(target=_run_agent, daemon=True).start()

    async def _generate():
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
        while True:
            try:
                event = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


@app.get("/api/chat/sessions")
async def list_chat_sessions():
    """List recent chat sessions."""
    store = SessionStore()
    try:
        return store.list_sessions()
    finally:
        store.close()


STATIC_DIR = os.path.join("frontend", "dist")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/{path:path}")
async def static_files(path: str):
    full = os.path.normpath(os.path.join(STATIC_DIR, path))
    # Prevent path traversal — resolved path must stay inside STATIC_DIR
    if not full.startswith(os.path.normpath(STATIC_DIR)):
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
    if os.path.isfile(full):
        return FileResponse(full)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
