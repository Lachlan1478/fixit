import re

_DENY_SHELL = [
    re.compile(r'\bgit\s+push\b', re.IGNORECASE),
    re.compile(r'\bdel\b', re.IGNORECASE),
    re.compile(r'\bformat\b', re.IGNORECASE),
]

_SAFE_SHELL = [
    re.compile(r'^\s*echo\b', re.IGNORECASE),
    re.compile(r'^\s*ls\b', re.IGNORECASE),
]


def decide_action(action: dict) -> str:
    """Return 'allow', 'deny', or 'review' for the given action dict."""
    action_type = action.get("type", "none")
    input_val = action.get("input", "")

    if action_type == "none":
        return "allow"

    if action_type == "read_file":
        return "allow"

    if action_type == "write_file":
        return "review"

    if action_type == "git_status":
        return "allow"

    if action_type == "git_diff":
        return "allow"

    if action_type == "git_add":
        return "allow"

    if action_type == "git_commit":
        return "review"

    if action_type == "shell":
        for pattern in _DENY_SHELL:
            if pattern.search(input_val):
                return "deny"
        for pattern in _SAFE_SHELL:
            if pattern.search(input_val):
                return "allow"
        return "review"

    if action_type == "browser_navigate":   return "allow"
    if action_type == "browser_click":      return "review"
    if action_type == "browser_fill":       return "review"
    if action_type == "browser_extract_text": return "allow"
    if action_type == "browser_screenshot": return "allow"

    return "review"
