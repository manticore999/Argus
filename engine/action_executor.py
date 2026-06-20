import logging
from typing import Any

from engine.toolkit import KubernetesToolkit

logger = logging.getLogger("argus.action_executor")

class ActionExecutor:
    """
    Secure execution handler that maps an approved "Action Request"
    to the corresponding mutating function in the KubernetesToolkit.
    """
    
    def __init__(self):
        self.toolkit = KubernetesToolkit()
        
        # Whitelist of allowed action tools (for security)
        self.allowed_actions = {
            "restart_pod",
            "scale_deployment",
            "rollout_restart_deployment"
        }

    def execute_approved_action(self, action_tool_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """
        Executes a mutating tool on the cluster, strictly enforcing the whitelist.
        """
        logger.info(f"Received request to execute approved action: {action_tool_name} with params {parameters}")
        
        if action_tool_name not in self.allowed_actions:
            error_msg = f"Security Violation: Action '{action_tool_name}' is not in the allowed actions whitelist."
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
            
        func = getattr(self.toolkit, action_tool_name, None)
        if not func or not callable(func):
            error_msg = f"Implementation Error: Action '{action_tool_name}' not found in toolkit."
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
            
        try:
            # Execute the actual mutation
            result = func(**parameters)
            logger.info(f"Action '{action_tool_name}' execution result: {result}")
            return result
        except Exception as e:
            logger.error(f"Action execution failed: {e}")
            return {"success": False, "error": str(e)}
