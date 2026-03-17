import logging
import os
import re

from tools import execute_tool

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_SHELL_BLOCKLIST = [
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
    if not abs_path.startswith(WORKSPACE_ROOT):
        raise ValueError(
            f"Path '{path}' resolves to '{abs_path}' which is outside workspace '{WORKSPACE_ROOT}'"
        )
    return abs_path


def _check_shell(command: str) -> None:
    """Check command against blocklist patterns. Raises ValueError if blocked."""
    for pattern in _SHELL_BLOCKLIST:
        if pattern.search(command):
            raise ValueError(f"Shell command blocked by safety filter: {command!r}")


async def execute_actions(actions: list[dict]) -> list[dict]:
    """Execute a list of action dicts. Returns list of result dicts."""
    results = []
    for action in actions:
        action_type = action.get("type", "none")
        input_val = action.get("input", "")

        try:
            if action_type == "none":
                results.append({"action": action, "result": "ok", "error": None})

            elif action_type == "read_file":
                abs_path = _check_path(input_val)
                result = await execute_tool("read_file", {"path": abs_path})
                results.append({"action": action, "result": result, "error": None})

            elif action_type == "write_file":
                abs_path = _check_path(input_val)
                content = action.get("content", "")
                result = await execute_tool("write_file", {"path": abs_path, "content": content})
                results.append({"action": action, "result": result, "error": None})

            elif action_type == "shell":
                _check_shell(input_val)
                result = await execute_tool("run_shell", {"command": input_val})
                results.append({"action": action, "result": result, "error": None})

            else:
                results.append({
                    "action": action,
                    "result": None,
                    "error": f"Unknown action type: {action_type!r}",
                })

        except ValueError as e:
            logger.warning("executor: safety check failed for action %r: %s", action_type, e)
            results.append({"action": action, "result": None, "error": str(e)})
        except Exception as e:
            logger.error("executor: action %r failed: %s", action_type, e)
            results.append({"action": action, "result": None, "error": str(e)})

    return results


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
            lines.append("Result:")
            lines.append(str(item["result"]) if item["result"] is not None else "(no output)")
        lines.append("")

    lines.append(
        'Please continue. If you have all the information you need, '
        'set actions to [{"type":"none","input":"","reason":"task complete"}] '
        'and include your final_answer.'
    )
    return "\n".join(lines)
