import logging
import os
import re

import middleware
import pending_store
from control import decide_action
from tools import execute_tool
from tools.browser import check_url

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_SHELL_BLOCKLIST = [
    re.compile(r'\bgit\s+push\b', re.IGNORECASE),
    re.compile(r'\brm\s+(-[a-z]*f[a-z]*\s|.*-[a-z]*f\b)', re.IGNORECASE),
    re.compile(r'\brmdir\s+/s\b', re.IGNORECASE),
    re.compile(r'\brd\s+/s\b', re.IGNORECASE),
    re.compile(r'\bformat\s+[a-z]:\b', re.IGNORECASE),
    re.compile(r'\bdel\s+/[sqf]', re.IGNORECASE),
    re.compile(r'\bshutdown\b', re.IGNORECASE),
    re.compile(r'\breboot\b', re.IGNORECASE),
    re.compile(r'\bmkfs\b', re.IGNORECASE),
    re.compile(r'\bdd\s+if=', re.IGNORECASE),
    re.compile(r':\(\)\s*\{.*\}', re.IGNORECASE),
    re.compile(r'\b(curl|wget)\b.*\|\s*(ba)?sh\b', re.IGNORECASE),
]


def _check_path(path: str) -> str:
    """Resolve path and verify it's within WORKSPACE_ROOT. Returns absolute path."""
    abs_path = os.path.abspath(path)
    if not abs_path.startswith(WORKSPACE_ROOT + os.sep) and abs_path != WORKSPACE_ROOT:
        raise ValueError(
            f"Path '{path}' resolves to '{abs_path}' which is outside workspace '{WORKSPACE_ROOT}'"
        )
    return abs_path


def _check_git_path(path: str) -> str:
    """Resolve a path relative to WORKSPACE_ROOT and verify it stays inside.
    Returns a path relative to WORKSPACE_ROOT (suitable for passing to git)."""
    if os.path.isabs(path):
        abs_path = os.path.normpath(path)
    else:
        abs_path = os.path.normpath(os.path.join(WORKSPACE_ROOT, path))
    if not abs_path.startswith(WORKSPACE_ROOT + os.sep) and abs_path != WORKSPACE_ROOT:
        raise ValueError(f"Git path '{path}' is outside workspace '{WORKSPACE_ROOT}'")
    return os.path.relpath(abs_path, WORKSPACE_ROOT)


def _check_shell(command: str) -> None:
    """Check command against blocklist patterns. Raises ValueError if blocked."""
    for pattern in _SHELL_BLOCKLIST:
        if pattern.search(command):
            raise ValueError(f"Shell command blocked by safety filter: {command!r}")


async def _dispatch_action(action: dict) -> dict:
    """Execute a single action, applying safety checks but skipping the control layer.

    Used for both allowed actions in execute_actions and for approved actions in
    execute_approved_action. Returns a result dict (never raises).
    """
    action_type = action.get("type", "none")
    input_val = action.get("input", "")

    try:
        if action_type == "none":
            return {"action": action, "result": "ok", "error": None, "decision": "allow"}

        elif action_type == "read_file":
            abs_path = _check_path(input_val)
            result = await execute_tool("read_file", {"path": abs_path})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "write_file":
            abs_path = _check_path(input_val)
            content = action.get("content", "")
            result = await execute_tool("write_file", {"path": abs_path, "content": content})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "shell":
            _check_shell(input_val)
            result = await execute_tool("run_shell", {"command": input_val})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "git_status":
            result = await execute_tool("git_status", {"workspace": WORKSPACE_ROOT})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "git_diff":
            rel_path = _check_git_path(input_val) if input_val else ""
            result = await execute_tool("git_diff", {"workspace": WORKSPACE_ROOT, "path": rel_path})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "git_add":
            rel_path = _check_git_path(input_val)
            result = await execute_tool("git_add", {"workspace": WORKSPACE_ROOT, "path": rel_path})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "git_commit":
            result = await execute_tool("git_commit", {"workspace": WORKSPACE_ROOT, "message": input_val})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "browser_navigate":
            check_url(input_val)
            result = await execute_tool("browser_navigate", {"url": input_val})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "browser_click":
            result = await execute_tool("browser_click", {"selector": input_val})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "browser_fill":
            text = action.get("content", "")
            result = await execute_tool("browser_fill", {"selector": input_val, "text": text})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "browser_extract_text":
            result = await execute_tool("browser_extract_text", {})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        elif action_type == "browser_screenshot":
            result = await execute_tool("browser_screenshot", {"filename_hint": input_val})
            return {"action": action, "result": result, "error": None, "decision": "allow"}

        else:
            return {
                "action": action,
                "result": None,
                "error": f"Unknown action type: {action_type!r}",
                "decision": "allow",
            }

    except ValueError as e:
        logger.warning("executor: safety check failed for action %r: %s", action_type, e)
        return {"action": action, "result": None, "error": str(e), "decision": "allow"}
    except Exception as e:
        logger.error("executor: action %r failed: %s", action_type, e)
        return {"action": action, "result": None, "error": str(e), "decision": "allow"}


async def execute_actions(actions: list[dict]) -> list[dict]:
    """Execute a list of action dicts. Returns list of result dicts.

    Denied actions produce error results and are skipped.
    Review actions are saved to pending_store and produce a review_required result.
    Allowed actions are dispatched normally.
    """
    results = []
    for action in actions:
        action_type = action.get("type", "none")
        input_val = action.get("input", "")

        decision = decide_action(action)
        middleware.log_action_decision(action, decision)

        if decision == "deny":
            results.append({
                "action": action,
                "result": None,
                "error": f"Action denied by policy (type={action_type}, input={input_val!r})",
                "decision": "deny",
            })
            continue

        if decision == "review":
            entry_id = pending_store.add_pending(action)
            results.append({
                "action": action,
                "result": {
                    "type": "review_required",
                    "id": entry_id,
                    "message": "This action requires approval",
                },
                "error": None,
                "decision": "review",
            })
            continue

        # decision == "allow"
        result = await _dispatch_action(action)
        results.append(result)

    return results


async def execute_approved_action(action: dict) -> dict:
    """Execute a previously-reviewed action, bypassing the control layer.

    Still applies executor safety checks (_check_path / _check_shell).
    """
    return await _dispatch_action(action)


def format_results_for_claude(results: list[dict]) -> str:
    """Format execution results as a follow-up message for Claude."""
    lines = ["Here are the results of your requested actions:\n"]
    for i, item in enumerate(results, start=1):
        action = item["action"]
        action_type = action.get("type", "?")
        input_val = action.get("input", "")
        lines.append(f"[Action {i}] type={action_type}  input={input_val!r}")
        if item["error"] is not None:
            lines.append(f"Error: {item['error']}")
        else:
            result = item["result"]
            if isinstance(result, dict) and result.get("type") == "review_required":
                lines.append(
                    f"Pending review (id={result['id']}): {result['message']}"
                )
            else:
                lines.append("Result:")
                lines.append(str(result) if result is not None else "(no output)")
        lines.append("")

    lines.append(
        'Please continue. If you have all the information you need, '
        'set actions to [{"type":"none","input":"","reason":"task complete"}] '
        'and include your final_answer.'
    )
    return "\n".join(lines)
