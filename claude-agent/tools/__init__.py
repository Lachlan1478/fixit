from tools.browser import (
    browser_click,
    browser_extract_text,
    browser_fill,
    browser_navigate,
    browser_screenshot,
)
from tools.filesystem import read_file, write_file, list_files
from tools.git import git_status, git_diff, git_add, git_commit
from tools.shell import run_shell

_REGISTRY = {
    "read_file": read_file,
    "write_file": write_file,
    "list_files": list_files,
    "run_shell": run_shell,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_add": git_add,
    "git_commit": git_commit,
    "browser_navigate": browser_navigate,
    "browser_click": browser_click,
    "browser_fill": browser_fill,
    "browser_extract_text": browser_extract_text,
    "browser_screenshot": browser_screenshot,
}


async def execute_tool(name: str, args: dict) -> str:
    handler = _REGISTRY.get(name)
    if not handler:
        return f"ERROR: Unknown tool '{name}'. Available: {', '.join(_REGISTRY)}"
    try:
        return await handler(**args)
    except TypeError as e:
        return f"ERROR: Invalid arguments for '{name}': {e}"
    except Exception as e:
        return f"ERROR: Tool '{name}' failed: {e}"
