from __future__ import annotations

import json
import logging
from typing import Any, TypedDict, List

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph

from config import GOOGLE_API_KEY, GEMINI_MODEL, RotatingGenAIClient
from engine.toolkit import KubernetesToolkit

logger = logging.getLogger("argus.agent_graph")

# =========================================================================
# SCHEMAS
# =========================================================================

class ToolRequest(BaseModel):
    tool_name: str = Field(description="Name of the tool to execute")
    parameters: dict[str, Any] = Field(description="Parameters to pass to the tool")

class ToolSelection(BaseModel):
    tools_to_call: list[ToolRequest] = Field(description="List of read-only diagnostic tools to execute")
    reasoning: str = Field(description="Why you selected these tools")

class SuggestedAction(BaseModel):
    action_tool_name: str = Field(description="The exact name of the action tool to run (e.g. restart_pod, scale_deployment)")
    parameters: dict[str, Any] = Field(description="Parameters required for the action tool")
    explanation: str = Field(description="Why this action will fix the problem")

class RemediationPlan(BaseModel):
    root_cause: str = Field(description="Detailed explanation of the root cause based on the tool evidence")
    confidence: str = Field(description="High, Medium, or Low")
    suggested_actions: list[SuggestedAction] = Field(description="A list of executable actions the user can approve to fix the issue")

# =========================================================================
# STATE
# =========================================================================

class AgentState(TypedDict):
    incident: dict
    tools_to_call: list[ToolRequest]
    tool_results: dict[str, Any]
    remediation_plan: RemediationPlan | None

# =========================================================================
# AGENT NODES
# =========================================================================

class LangGraphAgent:
    """
    Implements a LangGraph DAG to handle incident triage using LangChain paradigms.
    """
    
    def __init__(self):
        self.toolkit = KubernetesToolkit()
        self.model = GEMINI_MODEL
        try:
            self._client = RotatingGenAIClient()
        except Exception as e:
            self._client = None
            logger.error(f"Failed to initialize RotatingGenAIClient: {e}")
            
        # Build the LangGraph DAG
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(AgentState)

        # Add Nodes
        workflow.add_node("decide_tools", self.node_decide_tools)
        workflow.add_node("collect_diagnostics", self.node_collect_diagnostics)
        workflow.add_node("generate_plan", self.node_generate_plan)

        # Define Edges
        workflow.set_entry_point("decide_tools")
        workflow.add_edge("decide_tools", "collect_diagnostics")
        workflow.add_edge("collect_diagnostics", "generate_plan")
        workflow.set_finish_point("generate_plan")

        return workflow.compile()

    def run_triage(self, incident: dict) -> RemediationPlan | None:
        if not self._client:
            return None

        logger.info(f"Starting LangGraph triage for incident: {incident['title']}")
        
        # Initialize Graph State
        initial_state: AgentState = {
            "incident": incident,
            "tools_to_call": [],
            "tool_results": {},
            "remediation_plan": None
        }
        
        # Execute DAG
        final_state = self.graph.invoke(initial_state)
        return final_state.get("remediation_plan")

    def node_decide_tools(self, state: AgentState) -> AgentState:
        """Node 1: Ask the LLM to select diagnostic tools."""
        incident = state["incident"]
        prompt = f"""
        You are a Kubernetes diagnostics expert. An incident has occurred.
        
        INCIDENT SUMMARY:
        Title: {incident.get('title')}
        Scope: {incident.get('scope')}
        Severity: {incident.get('severity')}
        
        Which diagnostic tools should we run to investigate this?
        
        AVAILABLE READ-ONLY TOOLS:
        - get_pod_logs(namespace: str, pod_name: str, lines: int=200)
        - get_pod_events(namespace: str, pod_name: str)
        - get_pod_status(namespace: str, pod_name: str)
        - get_deployment_status(namespace: str, deployment_name: str)
        
        Respond ONLY with a valid JSON matching the schema.
        """
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_json_schema": ToolSelection.model_json_schema(),
                },
            )
            selection = ToolSelection.model_validate_json(response.text)
            state["tools_to_call"] = selection.tools_to_call
        except Exception as e:
            logger.error(f"Tool selection failed: {e}")
            state["tools_to_call"] = []
            
        return state

    def node_collect_diagnostics(self, state: AgentState) -> AgentState:
        """Node 2: Execute the selected tools in parallel."""
        requests = state.get("tools_to_call", [])
        results = {}
        
        if not requests:
            logger.warning("No tools selected by LLM.")
            state["tool_results"] = {"error": "No tools selected"}
            return state

        for req in requests:
            func = getattr(self.toolkit, req.tool_name, None)
            if func and callable(func):
                logger.info(f"LangGraph executing tool: {req.tool_name}")
                try:
                    res = func(**req.parameters)
                    results[req.tool_name] = res
                except Exception as e:
                    results[req.tool_name] = {"success": False, "error": str(e)}
            else:
                results[req.tool_name] = {"success": False, "error": "Tool not found"}
                
        state["tool_results"] = results
        return state

    def node_generate_plan(self, state: AgentState) -> AgentState:
        """Node 3: Analyze the evidence and output a Remediation Plan."""
        incident = state["incident"]
        tool_evidence = state.get("tool_results", {})
        
        prompt = f"""
        You are a Kubernetes diagnostics expert. You have requested diagnostic data.
        
        INCIDENT: {incident.get('title')}
        EVIDENCE GATHERED:
        {json.dumps(tool_evidence, indent=2)}
        
        Based on this evidence, determine the root cause and propose a remediation plan.
        You may suggest the following ACTION TOOLS for the user to approve:
        - restart_pod(namespace: str, pod_name: str)
        - scale_deployment(namespace: str, deployment_name: str, replicas: int)
        - rollout_restart_deployment(namespace: str, deployment_name: str)
        
        Respond ONLY with a valid JSON matching the schema.
        """
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_json_schema": RemediationPlan.model_json_schema(),
                },
            )
            state["remediation_plan"] = RemediationPlan.model_validate_json(response.text)
        except Exception as e:
            logger.error(f"Remediation planning failed: {e}")
            state["remediation_plan"] = None
            
        return state

# Keep the original class name AgentGraph alias for compatibility with main.py
AgentGraph = LangGraphAgent
