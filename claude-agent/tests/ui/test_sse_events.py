"""
UI mock tests — every SSE event type → correct DOM state.

Uses Playwright with all API calls mocked via page.route(); /task returns
fake SSE events so no Claude subprocess is needed.
"""

import json

import pytest
from playwright.sync_api import expect


# Tiny 1×1 GIF for image-related tests
_GIF_1X1 = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!"
    b"\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


def _mock_images(page) -> None:
    page.route("**/image**", lambda r: r.fulfill(
        status=200, headers={"Content-Type": "image/gif"}, body=_GIF_1X1,
    ))


def _send(page, prompt: str, wait_selector: str, timeout: int = 10_000) -> None:
    page.locator("#prompt").fill(prompt)
    page.locator("#send-btn").click()
    page.wait_for_selector(wait_selector, timeout=timeout)


# ── status event ─────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_status_card(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {"type": "status", "message": "Ready (sonnet · 10 tools)", "elapsed_ms": 50},
        {"type": "done", "result": "ok"},
    ])
    _send(ui_page, "hello", ".feed-item.status")

    # A "Prompt" echo status card is added first; find the Init card by label text
    init_label = ui_page.locator(".feed-item.status .feed-label").filter(has_text="Init")
    expect(init_label).to_be_visible()


# ── tool event ───────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_tool_card(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {
            "type": "tool",
            "name": "Write",
            "summary": "Writing hello.py",
            "input": {"file_path": "hello.py"},
            "tool_use_id": "tid-001",
            "elapsed_ms": 1200,
        },
        {"type": "done", "result": "done"},
    ])
    _send(ui_page, "write hello.py", ".feed-item.tool")

    card = ui_page.locator(".feed-item.tool").first
    expect(card).to_be_visible()
    expect(card.locator(".feed-summary")).to_contain_text("Writing hello.py")
    # elapsed shown as "1.2s"
    expect(card.locator(".feed-time")).to_contain_text("1.2s")


# ── bash expand button ───────────────────────────────────────────────────────

@pytest.mark.ui
def test_bash_expand_button(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {
            "type": "tool",
            "name": "Bash",
            "summary": "Running: ls -la",
            "input": {"command": "ls -la"},
            "tool_use_id": "bash-001",
            "elapsed_ms": 300,
        },
        {
            "type": "tool_result",
            "tool_use_id": "bash-001",
            "name": "Bash",
            "content": "total 8\ndrwxr-xr-x 2 user group 64 Jan 1 00:00 .",
            "is_error": False,
        },
        {"type": "done", "result": "done"},
    ])
    _send(ui_page, "ls -la", ".feed-expand-btn")

    btn = ui_page.locator(".feed-expand-btn").first
    expect(btn).to_be_visible()
    expect(btn).to_contain_text("Show output")

    btn.click()
    expect(ui_page.locator(".bash-output.open").first).to_be_visible()


@pytest.mark.ui
def test_bash_error_state(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {
            "type": "tool",
            "name": "Bash",
            "summary": "Running: bad_cmd",
            "input": {"command": "bad_cmd"},
            "tool_use_id": "bash-err-001",
            "elapsed_ms": 100,
        },
        {
            "type": "tool_result",
            "tool_use_id": "bash-err-001",
            "name": "Bash",
            "content": "bash: bad_cmd: command not found",
            "is_error": True,
        },
        {"type": "done", "result": "done"},
    ])
    _send(ui_page, "run bad cmd", ".bash-err")

    btn = ui_page.locator(".feed-expand-btn").first
    expect(btn).to_contain_text("Show error")
    expect(ui_page.locator(".bash-err").first).to_be_visible()


# ── image thumbnail + lightbox ────────────────────────────────────────────────

@pytest.mark.ui
def test_image_thumbnail(ui_page, route_task_sse):
    _mock_images(ui_page)
    route_task_sse(ui_page, [
        {
            "type": "image",
            "path": "claude-agent/static/result.gif",
            "url": "/image?path=claude-agent/static/result.gif",
        },
        {"type": "done", "result": "done"},
    ])
    _send(ui_page, "show image", ".feed-item.image")

    card = ui_page.locator(".feed-item.image").first
    expect(card).to_be_visible()
    expect(card.locator("img.feed-thumb")).to_be_visible()

    # Click thumbnail → lightbox opens
    card.locator("img.feed-thumb").click()
    expect(ui_page.locator("#img-overlay")).to_have_class("open")


# ── todos panel ──────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_todos_panel(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {
            "type": "todos",
            "items": [
                {"id": "1", "content": "Write tests", "status": "completed"},
                {"id": "2", "content": "Run linter",  "status": "in_progress"},
            ],
        },
        {"type": "done", "result": "done"},
    ])
    _send(ui_page, "plan tasks", "#todos-section.visible")

    section = ui_page.locator("#todos-section")
    expect(section).to_have_class("visible")
    expect(section.locator(".todo-item")).to_have_count(2)
    expect(section.locator(".todo-item.completed")).to_have_count(1)
    expect(section.locator(".todo-item.in_progress")).to_have_count(1)


# ── text / thinking card ─────────────────────────────────────────────────────

@pytest.mark.ui
def test_text_thinking_card(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {"type": "text", "content": "Let me think about this carefully..."},
        {"type": "done", "result": "done"},
    ])
    _send(ui_page, "think", ".feed-item.text")

    card = ui_page.locator(".feed-item.text").first
    expect(card).to_be_visible()
    expect(card.locator(".feed-label")).to_contain_text("Thinking")


# ── done card ────────────────────────────────────────────────────────────────

@pytest.mark.ui
def test_done_card(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {"type": "done", "result": "Task completed successfully!"},
    ])
    _send(ui_page, "do task", ".feed-item.done")

    card = ui_page.locator(".feed-item.done").first
    expect(card).to_be_visible()
    expect(card.locator(".feed-label")).to_contain_text("Done")


# ── error card + toast ───────────────────────────────────────────────────────

@pytest.mark.ui
def test_error_card_and_toast(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {"type": "error", "message": "Something went terribly wrong"},
    ])
    _send(ui_page, "crash", ".feed-item.error")

    expect(ui_page.locator(".feed-item.error").first).to_be_visible()
    expect(ui_page.locator("#toast")).to_be_visible()
    expect(ui_page.locator("#toast")).to_contain_text("Something went terribly wrong")


# ── HTML preview auto-load ───────────────────────────────────────────────────

@pytest.mark.ui
def test_write_html_preview(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {
            "type": "tool",
            "name": "Write",
            "summary": "Writing color.html",
            "input": {"file_path": "claude-agent/static/color.html"},
            "tool_use_id": "tid-html",
            "elapsed_ms": 500,
        },
        {"type": "done", "result": "done"},
    ])
    _send(ui_page, "write html", "#preview-section.visible")

    expect(ui_page.locator("#preview-section")).to_have_class("visible")
    frame_src = ui_page.locator("#preview-frame").get_attribute("src")
    assert frame_src and "color.html" in frame_src


@pytest.mark.ui
def test_write_non_html_no_preview(ui_page, route_task_sse):
    route_task_sse(ui_page, [
        {
            "type": "tool",
            "name": "Write",
            "summary": "Writing utils.py",
            "input": {"file_path": "utils.py"},
            "tool_use_id": "tid-py",
            "elapsed_ms": 200,
        },
        {"type": "done", "result": "done"},
    ])
    _send(ui_page, "write python", ".feed-item.tool")

    # Preview should NOT be visible for a .py file
    expect(ui_page.locator("#preview-section")).not_to_have_class("visible")


# ── tree refresh on Write event ───────────────────────────────────────────────

@pytest.mark.ui
def test_write_triggers_tree_refresh(ui_page, route_task_sse):
    """After a Write event the UI schedules a tree refresh (debounce 1.5 s)."""
    tree_calls = []

    refreshed_tree = json.dumps({
        "path": "",
        "dirs": [],
        "files": [{"name": "newfile.py", "path": "newfile.py", "size": 100}],
    })

    def _tree_handler(route):
        tree_calls.append(1)
        route.fulfill(status=200, content_type="application/json", body=refreshed_tree)

    # Replace the default files stub with our counting version
    ui_page.route("**/files**", _tree_handler)
    tree_calls.clear()  # ignore calls made during page load

    route_task_sse(ui_page, [
        {
            "type": "tool",
            "name": "Write",
            "summary": "Writing newfile.py",
            "input": {"file_path": "newfile.py"},
            "tool_use_id": "tid-refresh",
            "elapsed_ms": 100,
        },
        {"type": "done", "result": "done"},
    ])

    ui_page.locator("#prompt").fill("write file")
    ui_page.locator("#send-btn").click()

    # Wait up to 4 s for the debounced refresh (1.5 s) to fire
    ui_page.wait_for_function(
        "() => document.querySelector('.exp-name.file') !== null",
        timeout=5_000,
    )
    assert len(tree_calls) > 0


# ── full event sequence ───────────────────────────────────────────────────────

@pytest.mark.ui
def test_full_sequence(ui_page, route_task_sse):
    _mock_images(ui_page)
    route_task_sse(ui_page, [
        {"type": "status", "message": "Ready", "elapsed_ms": 10},
        {
            "type": "tool", "name": "Write", "summary": "Writing app.py",
            "input": {"file_path": "app.py"}, "tool_use_id": "t1", "elapsed_ms": 100,
        },
        {
            "type": "image",
            "path": "out/result.gif",
            "url": "/image?path=out/result.gif",
        },
        {"type": "todos", "items": [{"id": "1", "content": "Step 1", "status": "completed"}]},
        {"type": "done", "result": "finished"},
    ])
    _send(ui_page, "full run", ".feed-item.done")

    # todos renders to #todos-section, NOT a .feed-item — so expect 4 feed items
    items = ui_page.locator(".feed-item")
    assert items.count() >= 4

    types_seen = set()
    for i in range(items.count()):
        cls = items.nth(i).get_attribute("class") or ""
        for t in ("status", "tool", "image", "done"):
            if t in cls:
                types_seen.add(t)

    assert types_seen >= {"status", "tool", "image", "done"}
    # Todos panel should be visible
    expect(ui_page.locator("#todos-section")).to_have_class("visible")
