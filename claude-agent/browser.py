"""Launch Chromium without touching the user's real ~/Library.

macOS attributes everything the agent runs to its python process; a Chromium
that probes other browsers' profiles trips the "python3.12 would like to
access data from other apps" dialog. Give it a throwaway HOME and profile and
it has nothing to probe.

    from browser import launch_options
    browser = await p.chromium.launch(**launch_options())
"""

import os
import tempfile

QUIET_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--use-mock-keychain",
    "--password-store=basic",
    "--disable-sync",
    "--disable-background-networking",
    "--disable-component-update",
]


def launch_options(headless: bool = True) -> dict:
    """Keyword arguments for `chromium.launch(...)` that keep Chromium inside a temp dir."""
    home = tempfile.mkdtemp(prefix="agent-chromium-")
    env = {
        k: v for k, v in os.environ.items() if k not in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")
    }
    env.update({"HOME": home, "XDG_CONFIG_HOME": home, "XDG_CACHE_HOME": home})
    return {
        "headless": headless,
        # No --user-data-dir: Playwright forbids it on launch(); with HOME redirected,
        # Chromium's default profile lands inside the temp dir anyway.
        "args": list(QUIET_ARGS),
        "env": env,
    }
