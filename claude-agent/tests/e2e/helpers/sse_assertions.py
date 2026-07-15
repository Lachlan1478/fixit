"""
Shared Playwright assertion helpers for SSE-driven UI state.

These functions wait for specific UI conditions that result from SSE events
streaming in from a real (or mocked) Claude session.
"""

from playwright.sync_api import Page, expect


def assert_feed_has_type(page: Page, event_type: str, timeout: int = 10_000) -> None:
    """Assert that at least one .feed-item of the given CSS class exists."""
    locator = page.locator(f".feed-item.{event_type}")
    expect(locator.first).to_be_visible(timeout=timeout)


def assert_preview_visible(page: Page, filename_fragment: str = "", timeout: int = 10_000) -> None:
    """Assert that the live preview section is visible and optionally check the iframe src."""
    expect(page.locator("#preview-section")).to_have_class("visible", timeout=timeout)
    if filename_fragment:
        src = page.locator("#preview-frame").get_attribute("src") or ""
        assert filename_fragment in src, (
            f"Expected preview iframe src to contain '{filename_fragment}', got '{src}'"
        )


def assert_image_feed_card_loaded(page: Page, timeout: int = 10_000) -> None:
    """Assert that at least one image thumbnail has loaded (naturalWidth > 0)."""
    page.wait_for_selector(".feed-item.image", timeout=timeout)
    loaded = page.evaluate("""() => {
        const imgs = document.querySelectorAll('img.feed-thumb');
        return Array.from(imgs).some(img => img.naturalWidth > 0);
    }""")
    assert loaded, "No image thumbnail has loaded (naturalWidth == 0 for all)"


def assert_todos_visible(page: Page, count: int | None = None, timeout: int = 10_000) -> None:
    """Assert the todos panel is visible and optionally check item count."""
    expect(page.locator("#todos-section")).to_have_class("visible", timeout=timeout)
    if count is not None:
        expect(page.locator(".todo-item")).to_have_count(count, timeout=timeout)


def assert_tree_contains(page: Page, filename: str, timeout: int = 10_000) -> None:
    """Assert that the project tree contains a node with the given filename."""
    page.wait_for_function(
        f"() => Array.from(document.querySelectorAll('.pt-name')).some(el => el.textContent.includes('{filename}'))",
        timeout=timeout,
    )


def wait_for_done(page: Page, timeout: int = 180_000) -> None:
    """Wait for the done feed card to appear."""
    page.wait_for_selector(".feed-item.done", timeout=timeout)


def wait_for_image_card(page: Page, timeout: int = 30_000) -> None:
    """Wait for at least one image feed card."""
    page.wait_for_selector(".feed-item.image", timeout=timeout)
