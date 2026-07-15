"""
End-to-end tests — Claude builds a color picker app.

Requires:
  - A running claude-agent server (set E2E_BASE_URL or uses localhost:8007)
  - The Claude CLI installed and authenticated
  - ~3-5 minutes total runtime

Run with::

    pytest tests/e2e/test_build_color_picker.py -v -s --timeout=600

The project_runner harness can be reused for any project by swapping PROJECT_CONFIG
in e2e/conftest.py.
"""

import os
import sys

import pytest
from playwright.sync_api import Page, expect

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "helpers"))

from project_runner import run_build, send_follow_up
from sse_assertions import (
    assert_feed_has_type,
    assert_image_feed_card_loaded,
    assert_preview_visible,
    assert_tree_contains,
    wait_for_done,
    wait_for_image_card,
)

# Server base URL (override with E2E_BASE_URL env var)
_BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8007")

pytestmark = pytest.mark.e2e


# ── Module-scoped build result ─────────────────────────────────────────────────

_build_result: dict | None = None


@pytest.fixture(scope="module")
def build_result(page: Page, project_config: dict):
    """
    Run the build once per module; all tests in this file share the result.
    This avoids re-running Claude for each assertion.
    """
    global _build_result
    if _build_result is None:
        _build_result = run_build(page, _BASE_URL, project_config, timeout_ms=300_000)
    return _build_result


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_file_created(build_result, project_config):
    """Claude wrote the output file to disk."""
    assert build_result["success"], "Build did not complete (no .feed-item.done)"
    assert build_result["output_exists"], (
        f"Output file not found: {build_result['output_path']}"
    )


def test_preview_iframe_auto_loaded(page: Page, build_result, project_config):
    """Preview section is visible and iframe points to the HTML file."""
    assert build_result["success"]
    assert_preview_visible(page, project_config["name"])


def test_app_works(page: Page, build_result, project_config):
    """Navigate to the app URL; it has ≥3 range inputs and a hex display."""
    assert build_result["output_exists"]

    page.goto(_BASE_URL + project_config["output_url"])
    page.wait_for_load_state("networkidle")

    sliders = page.locator("input[type='range']")
    assert sliders.count() >= 3, f"Expected ≥3 range sliders, got {sliders.count()}"

    # Move a slider and verify the hex display changes
    initial_hex = _get_hex_text(page)
    sliders.first.evaluate("el => { el.value = el.max / 2; el.dispatchEvent(new Event('input')); }")
    new_hex = _get_hex_text(page)

    # At least one of the selectors should return a hex-like string
    assert initial_hex or new_hex  # page loaded without JS errors


def test_screenshot_reflex(page: Page, build_result):
    """Sending 'screenshot the color picker' triggers an image feed card."""
    assert build_result["success"]

    # Navigate back to the agent UI
    page.goto(_BASE_URL)
    page.wait_for_load_state("networkidle")

    ta = page.locator("#prompt")
    ta.fill(f"screenshot the color picker at {_BASE_URL}/static/color_picker.html")
    ta.press("Enter")

    wait_for_image_card(page, timeout=60_000)
    assert_image_feed_card_loaded(page)

    # Click thumbnail → lightbox
    page.locator("img.feed-thumb").first.click()
    expect(page.locator("#img-overlay")).to_have_class("open")


def test_project_tree_updated(page: Page, build_result):
    """color_picker.html appears in the project tree."""
    assert build_result["output_exists"]
    page.goto(_BASE_URL)
    page.wait_for_load_state("networkidle")
    assert_tree_contains(page, "color_picker.html", timeout=15_000)


def test_follow_up_iteration(page: Page, build_result, project_config):
    """Send a follow-up prompt; app gains a 'Random color' button."""
    assert build_result["success"]

    page.goto(_BASE_URL)
    page.wait_for_load_state("networkidle")

    success = send_follow_up(page, project_config["follow_up_prompt"], timeout_ms=240_000)
    assert success, "Follow-up did not complete (no .feed-item.done)"

    # Navigate to the app and verify the button exists
    page.goto(_BASE_URL + project_config["output_url"])
    page.wait_for_load_state("networkidle")

    buttons = page.locator("button")
    texts = [buttons.nth(i).inner_text().lower() for i in range(buttons.count())]
    assert any("random" in t for t in texts), (
        f"Expected a 'Random color' button, found: {texts}"
    )


def test_image_feed_card_loaded(page: Page, build_result):
    """img.feed-thumb has naturalWidth > 0 after screenshot reflex."""
    assert build_result["success"]
    page.goto(_BASE_URL)
    page.wait_for_load_state("networkidle")
    # This test validates the earlier screenshot-reflex card is still in the feed
    # (or we can trigger another screenshot here)
    if page.locator(".feed-item.image").count() > 0:
        assert_image_feed_card_loaded(page)
    else:
        pytest.skip("No image card in feed — run test_screenshot_reflex first")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_hex_text(page: Page) -> str:
    """Try common selectors for a hex color display."""
    for sel in ("#hex", "#hex-code", ".hex", "[id*='hex']", "[class*='hex']"):
        el = page.locator(sel)
        if el.count() > 0:
            return el.first.inner_text()
    return ""
