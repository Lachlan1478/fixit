"""
Reusable build harness for E2E tests.

Pass any PROJECT_CONFIG dict to run_build() and it will:
  1. Navigate to the Claude Agent UI
  2. Reset the session
  3. Fill and send the build prompt
  4. Wait for a .feed-item.done card (up to 180 s)
  5. Return metadata about the run

Swap PROJECT_CONFIG to validate any project Claude is asked to build.
"""

import json
import os
import sys
import time

from playwright.sync_api import Page

# Ensure claude-agent modules are importable
_HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def reset_session(page: Page, base_url: str, agent_id: str = "default") -> None:
    """Call /reset_memory for the given agent via the API."""
    import httpx

    httpx.post(
        f"{base_url}/reset_memory",
        json={"agent_id": agent_id},
        timeout=10,
    )


def run_build(
    page: Page,
    base_url: str,
    config: dict,
    agent_id: str = "default",
    timeout_ms: int = 180_000,
) -> dict:
    """
    Drive the Claude Agent UI to build the project described in *config*.

    Returns::

        {
            "success": bool,          # True if .feed-item.done appeared
            "elapsed_s": float,       # Wall-clock seconds
            "output_exists": bool,    # True if output_path exists on disk
        }
    """
    # 1. Navigate to UI
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    # 2. Reset session to start fresh
    try:
        reset_session(page, base_url, agent_id)
        page.locator("#reset-btn").click()
        page.wait_for_timeout(500)
    except Exception:
        pass   # best-effort reset

    # 3. Fill and submit the build prompt
    ta = page.locator("#prompt")
    ta.fill(config["build_prompt"])

    t0 = time.monotonic()
    ta.press("Enter")

    # 4. Wait for completion
    success = False
    try:
        page.wait_for_selector(".feed-item.done", timeout=timeout_ms)
        success = True
    except Exception:
        pass

    elapsed = round(time.monotonic() - t0, 1)

    # 5. Check output file
    workspace_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    output_abs = os.path.join(workspace_root, config["output_path"])
    output_exists = os.path.isfile(output_abs)

    return {
        "success": success,
        "elapsed_s": elapsed,
        "output_exists": output_exists,
        "output_path": output_abs,
    }


def send_follow_up(
    page: Page,
    prompt: str,
    timeout_ms: int = 120_000,
) -> bool:
    """
    Send a follow-up prompt in the current session and wait for done.
    Returns True if done card appeared.
    """
    ta = page.locator("#prompt")
    ta.fill(prompt)
    ta.press("Enter")

    try:
        page.wait_for_selector(".feed-item.done", timeout=timeout_ms)
        return True
    except Exception:
        return False
