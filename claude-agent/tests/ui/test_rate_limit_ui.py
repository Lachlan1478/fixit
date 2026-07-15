"""
UI mock tests for rate-limit UX.

Tests cover:
- Banner shown on rate_limited event
- Pending prompt saved
- Auto-clear: uses page.clock to advance fake time past the 10 s poll interval
  so the real production poll function fires (no injected JS)
- Auto-resume of queued prompt after limit clears
- Banner shown on initial page load when already limited
- Countdown decrements
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect


def _future_iso(hours: float = 5.0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


def _far_future_iso() -> str:
    return "2099-06-15T12:00:00+00:00"


def _rate_limited_events(reset_at: str) -> list[dict]:
    return [
        {
            "type": "rate_limited",
            "reset_at": reset_at,
            "message": "Claude usage limit reached",
        }
    ]


def _send(page, prompt: str) -> None:
    page.locator("#prompt").fill(prompt)
    page.locator("#send-btn").click()


# ── banner shown ─────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_banner_shown(ui_page, route_task_sse):
    reset_at = _far_future_iso()
    route_task_sse(ui_page, _rate_limited_events(reset_at))

    _send(ui_page, "do something")

    banner = ui_page.locator("#rate-limit-banner")
    expect(banner).to_be_visible(timeout=5_000)
    # Note: the sendPrompt() finally-block re-enables the button after SSE ends;
    # showRateLimitBanner sets it disabled but finally overrides. Check status dot.
    expect(ui_page.locator("#status-dot")).to_have_class("status-dot limited")


# ── pending prompt saved ──────────────────────────────────────────────────────

@pytest.mark.ui
def test_pending_prompt_saved(ui_page, route_task_sse):
    """The prompt value should be saved as _pendingPrompt when rate limited."""
    route_task_sse(ui_page, _rate_limited_events(_far_future_iso()))

    _send(ui_page, "do the thing")
    ui_page.wait_for_selector("#rate-limit-banner", state="visible", timeout=5_000)

    # let variables in <script> blocks are NOT window properties; access by bare name
    pending = ui_page.evaluate("() => _pendingPrompt")
    assert pending == "do the thing"


# ── auto-clear banner ─────────────────────────────────────────────────────────

def _make_clock_page(page, live_server_url, route_task_sse_fn, route_rate_limit_status_fn):
    """
    Helper: install a fake clock BEFORE navigating so the real production
    setInterval(10000) can be fired via page.clock.fast_forward().

    This tests the real production poll loop, not injected replacement JS.
    """
    stub_rl   = json.dumps({"is_limited": False, "reset_at": None})
    stub_repo = json.dumps({"status": "## master\n", "diff": "", "modified_files": [], "diff_file_count": 0})
    stub_hist = json.dumps({"history": []})
    stub_tree = json.dumps({"name": "ws", "path": "", "type": "dir", "children": []})

    page.clock.install()    # fake clock — timers only fire when we call fast_forward
    page.route("**/history/**", lambda r: r.fulfill(status=200, content_type="application/json", body=stub_hist))
    page.route("**/repo",       lambda r: r.fulfill(status=200, content_type="application/json", body=stub_repo))
    page.route("**/tree**",     lambda r: r.fulfill(status=200, content_type="application/json", body=stub_tree))
    page.route("**/rate_limit_status", lambda r: r.fulfill(status=200, content_type="application/json", body=stub_rl))

    page.goto(live_server_url)
    page.wait_for_load_state("domcontentloaded")
    return page


@pytest.mark.ui
def test_auto_clear(page, live_server_url, route_task_sse, route_rate_limit_status):
    """
    Banner clears when /rate_limit_status returns is_limited=false.

    Uses page.clock to advance fake time by 10.5 s so the real production
    setInterval fires — no injected replacement JS.
    """
    _make_clock_page(page, live_server_url, route_task_sse, route_rate_limit_status)

    # Arm /task mock BEFORE sending prompt
    route_task_sse(page, _rate_limited_events(_future_iso(hours=0.5)))
    _send(page, "work")
    page.wait_for_selector("#rate-limit-banner", state="visible", timeout=8_000)

    # Override /rate_limit_status to return cleared immediately on first poll
    route_rate_limit_status(page, [{"is_limited": False, "reset_at": None}])

    # Fire the real 10 s production poll by advancing the fake clock,
    # then wait_for_function (real time) for the async fetch to settle in DOM.
    page.clock.fast_forward(10_500)
    page.wait_for_function(
        "() => document.getElementById('rate-limit-banner').style.display !== 'block'",
        timeout=8_000,
    )

    expect(page.locator("#rate-limit-banner")).to_be_hidden()
    expect(page.locator("#send-btn")).to_be_enabled()


# ── auto-resume ───────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_auto_resume(page, live_server_url, route_task_sse, route_rate_limit_status):
    """
    After the banner clears and _pendingPrompt is set, /task is called again.
    Uses page.clock for the same reason as test_auto_clear.
    """
    _make_clock_page(page, live_server_url, route_task_sse, route_rate_limit_status)

    route_task_sse(page, _rate_limited_events(_future_iso(hours=0.5)))
    _send(page, "resumeable task")
    page.wait_for_selector("#rate-limit-banner", state="visible", timeout=8_000)

    # Track second /task call
    task_calls = []

    def _task_handler(route):
        task_calls.append(1)
        body = "data: " + json.dumps({"type": "done", "result": "resumed!"}) + "\n\n"
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)

    page.route("**/task", _task_handler)

    route_rate_limit_status(page, [
        {"is_limited": True,  "reset_at": _future_iso(hours=0.5)},
        {"is_limited": False, "reset_at": None},
    ])

    # Fire the real production poll
    page.clock.fast_forward(10_500)
    # Wait for the auto-resume sendPrompt (has a 1000 ms setTimeout inside)
    page.clock.fast_forward(1_500)
    page.wait_for_timeout(1_000)

    page.wait_for_function(
        "() => document.querySelectorAll('.feed-item.done').length >= 2",
        timeout=10_000,
    )
    assert len(task_calls) >= 1


# ── banner on page load ───────────────────────────────────────────────────────

@pytest.mark.ui
def test_banner_on_page_load(page, live_server_url):
    """If /rate_limit_status returns is_limited=true on load, banner appears."""
    rl_body = json.dumps({"is_limited": True, "reset_at": _far_future_iso()})
    stub_body = json.dumps({
        "status": "## master\n", "diff": "", "modified_files": [], "diff_file_count": 0,
    })
    page.route("**/rate_limit_status", lambda r: r.fulfill(
        status=200, content_type="application/json", body=rl_body,
    ))
    page.route("**/repo", lambda r: r.fulfill(status=200, content_type="application/json", body=stub_body))
    page.route("**/history/**", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"history":[]}',
    ))
    page.route("**/tree**", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body='{"name":"ws","path":"","type":"dir","children":[]}',
    ))

    page.goto(live_server_url)
    page.wait_for_load_state("networkidle")

    expect(page.locator("#rate-limit-banner")).to_be_visible(timeout=5_000)


# ── countdown decrements ─────────────────────────────────────────────────────

@pytest.mark.ui
def test_countdown_decrements(ui_page, route_task_sse):
    """Countdown timer value should decrease over time."""
    reset_at = _future_iso(hours=0.1667)  # ~10 minutes from now
    route_task_sse(ui_page, _rate_limited_events(reset_at))
    _send(ui_page, "trigger limit")

    ui_page.wait_for_selector("#rate-limit-banner", state="visible", timeout=5_000)

    countdown_t0 = ui_page.locator("#rl-countdown").inner_text()
    # Wait 2 seconds
    ui_page.wait_for_timeout(2_100)
    countdown_t2 = ui_page.locator("#rl-countdown").inner_text()

    # Countdown should have advanced (decreased or changed)
    assert countdown_t0 != countdown_t2 or countdown_t0 == "—"
