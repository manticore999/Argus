"""
Centralised configuration for the AIOps platform.

Loads values from .env (via python-dotenv) then exposes them as module-level
constants.  Every other module should ``from config import ...`` instead of
reading os.environ or hardcoding values.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (parent of this file's directory)
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)

GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL: str   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

# Load comma-separated keys from env for key rotation support
_keys_env = os.getenv("GEMINI_API_KEYS", "") or os.getenv("GOOGLE_API_KEYS", "")
API_KEYS: list[str] = [k.strip() for k in _keys_env.split(",") if k.strip()]

if GOOGLE_API_KEY and GOOGLE_API_KEY not in API_KEYS:
    API_KEYS.insert(0, GOOGLE_API_KEY)

_current_key_idx = 0

def get_api_key() -> str:
    global _current_key_idx
    if not API_KEYS:
        return GOOGLE_API_KEY
    return API_KEYS[_current_key_idx]

def rotate_key() -> bool:
    global _current_key_idx
    if _current_key_idx + 1 < len(API_KEYS):
        _current_key_idx += 1
        print(f"[LLM] Rotating API key to slot {_current_key_idx} ({get_api_key()[:8]}...)")
        return True
    return False

class RotatingServiceWrapper:
    def __init__(self, client_wrapper, service_name):
        self._client_wrapper = client_wrapper
        self._service_name = service_name

    def __getattr__(self, method_name):
        def wrapper(*args, **kwargs):
            return self._client_wrapper._execute_with_retry(self._service_name, method_name, *args, **kwargs)
        return wrapper

class RotatingGenAIClient:
    def __init__(self):
        self.models = RotatingServiceWrapper(self, "models")
        self.chats = RotatingServiceWrapper(self, "chats")
        self.files = RotatingServiceWrapper(self, "files")

    def __bool__(self):
        return bool(get_api_key())

    def _execute_with_retry(self, service_name, method_name, *args, **kwargs):
        import time
        import google.genai as genai
        max_retries = len(API_KEYS) or 1
        for attempt in range(max_retries):
            key = get_api_key()
            if not key:
                raise ValueError("No API key available for Gemini Client")
            try:
                client = genai.Client(api_key=key)
                service = getattr(client, service_name)
                method = getattr(service, method_name)
                return method(*args, **kwargs)
            except Exception as e:
                err_msg = str(e).upper()
                if "RESOURCE_EXHAUSTED" in err_msg or "429" in err_msg or "QUOTA" in err_msg:
                    print(f"[LLM] Quota exhausted on key {key[:8]}... - rotating key")
                    if rotate_key():
                        time.sleep(1)
                        continue
                raise e

# ── Prometheus ───────────────────────────────────────────────────────────────

PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

# ── Database ─────────────────────────────────────────────────────────────────

DB_PATH: str = os.getenv("DB_PATH", "data/aiops.db")
