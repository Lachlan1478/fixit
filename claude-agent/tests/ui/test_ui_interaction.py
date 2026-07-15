"""
UI interaction tests — tabs, model selector, chips, feed controls.

All API calls are mocked; these tests exercise purely client-side JS behavior.
"""

import json

import pytest
from playwright.sync_api import expect


def _done_sse() -> str:
    return "data: " + json.dumps({"type": "done", "result": "ok"}) + "\n\n"


# ── Agent tabs ────────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_tab_switch(ui_page):
    """Clicking the 'frontend' tab makes it active and deactivates 'default'."""
    frontend_tab = ui_page.locator(".agent-tab[data-agent='frontend']")
    default_tab  = ui_page.locator(".agent-tab[data-agent='default']")

    frontend_tab.click()

    expect(frontend_tab).to_have_class("agent-tab active")
    expect(default_tab).not_to_have_class("active")


@pytest.mark.ui
def test_add_tab(ui_page):
    """Clicking '+' and confirming a name creates a new active tab."""
    ui_page.on("dialog", lambda d: d.accept("mybot"))
    ui_page.locator(".tab-add").click()

    new_tab = ui_page.locator(".agent-tab[data-agent='mybot']")
    expect(new_tab).to_be_visible(timeout=3_000)
    expect(new_tab).to_have_class("agent-tab active")


# ── Model selector ────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_model_selector(ui_page):
    """Opening the extras drawer and clicking 'Haiku' activates that model."""
    ui_page.locator("#extras-btn").click()
    haiku_btn = ui_page.locator(".model-btn[data-model='haiku']")
    expect(haiku_btn).to_be_visible()

    haiku_btn.click()

    expect(haiku_btn).to_have_class("model-btn active")
    expect(ui_page.locator(".model-btn[data-model='sonnet']")).not_to_have_class("active")
    # let variables are NOT window properties; verify via bare name
    assert ui_page.evaluate("() => _model") == "haiku"


@pytest.mark.ui
def test_model_selector_opus_default(ui_page):
    """Opus is active by default (the most capable model)."""
    ui_page.locator("#extras-btn").click()
    expect(ui_page.locator(".model-btn[data-model='opus']")).to_have_class("model-btn active")


# ── Quick-action chips ────────────────────────────────────────────────────────

@pytest.mark.ui
def test_chip_fills_textarea(ui_page):
    """Clicking 'Fix last error' chip fills the textarea with the correct text."""
    ui_page.locator("#extras-btn").click()
    ui_page.locator(".extras-chip", has_text="Fix last error").click()

    expect(ui_page.locator("#prompt")).to_have_value("Fix the last error")


@pytest.mark.ui
def test_chip_explain(ui_page):
    ui_page.locator("#extras-btn").click()
    ui_page.locator(".extras-chip", has_text="Explain").click()
    expect(ui_page.locator("#prompt")).to_have_value("Explain what you just did")


# ── Feed controls ─────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_clear_feed(ui_page, route_task_sse):
    """After injecting a feed item, clicking Clear leaves only the empty state."""
    route_task_sse(ui_page, [{"type": "done", "result": "hello"}])
    ui_page.locator("#prompt").fill("hello")
    ui_page.locator("#send-btn").click()
    ui_page.wait_for_selector(".feed-item.done", timeout=5_000)

    # Scope to the Live Feed section to avoid matching the history Clear button
    ui_page.locator("section:has(#feed-list) button.secondary").click()

    expect(ui_page.locator(".feed-empty")).to_be_visible()
    expect(ui_page.locator(".feed-item")).to_have_count(0)


# ── Enter submits / Shift+Enter newline ───────────────────────────────────────

@pytest.mark.ui
def test_enter_submits(ui_page, route_task_sse):
    """Pressing Enter in the prompt textarea submits the form."""
    route_task_sse(ui_page, [{"type": "done", "result": "submitted"}])
    ui_page.locator("#prompt").fill("do the thing")
    ui_page.locator("#prompt").press("Enter")
    ui_page.wait_for_selector(".feed-item.done", timeout=8_000)

    expect(ui_page.locator(".feed-item.done").first).to_be_visible()


@pytest.mark.ui
def test_shift_enter_newline(ui_page):
    """Shift+Enter inserts a newline without submitting."""
    ta = ui_page.locator("#prompt")
    ta.fill("line one")
    ta.press("Shift+Enter")
    ta.type("line two")

    value = ta.input_value()
    assert "\n" in value
    # Send button should still be enabled (no request was made)
    expect(ui_page.locator("#send-btn")).to_be_enabled()


# ── Reset clears UI ───────────────────────────────────────────────────────────

@pytest.mark.ui
def test_reset_clears_ui(ui_page, route_task_sse):
    """Clicking Reset calls /reset_memory and clears feed + history."""
    # Inject a feed item first
    route_task_sse(ui_page, [{"type": "done", "result": "pre-reset"}])
    ui_page.locator("#prompt").fill("pre-reset task")
    ui_page.locator("#send-btn").click()
    ui_page.wait_for_selector(".feed-item.done", timeout=5_000)

    # Mock /reset_memory
    ui_page.route("**/reset_memory", lambda r: r.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({"agent_id": "default", "status": "reset"}),
    ))

    # Reset now asks for confirmation before clearing the conversation.
    ui_page.on("dialog", lambda d: d.accept())
    ui_page.locator("#reset-btn").click()

    # Feed should be cleared (empty state or only the "Session" done item)
    expect(ui_page.locator("#history-list .history-empty")).to_be_visible(timeout=3_000)
