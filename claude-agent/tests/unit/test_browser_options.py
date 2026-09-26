"""Quiet Chromium launch options keep the browser out of ~/Library."""

import os

import browser


def test_launch_options_use_a_throwaway_home_and_profile():
    opts = browser.launch_options()
    home = opts["env"]["HOME"]
    assert home != os.path.expanduser("~") and os.path.isdir(home)
    assert opts["env"]["XDG_CONFIG_HOME"] == home
    assert not any(a.startswith("--user-data-dir") for a in opts["args"])  # Playwright rejects it
    for flag in ("--use-mock-keychain", "--password-store=basic", "--no-first-run"):
        assert flag in opts["args"]
    assert opts["headless"] is True
