"""Playwright browser automation tools."""
import asyncio
import ipaddress
import logging
import os
import re
import tempfile
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BROWSER_TIMEOUT = 30_000  # ms

_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

_playwright = None
_browser = None
_page = None
_lock = asyncio.Lock()


def check_url(url: str) -> None:
    """Raise ValueError if the URL scheme is not http/https or hostname is private/loopback."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Scheme {parsed.scheme!r} not allowed. Only http and https are permitted.")
    hostname = parsed.hostname or ""
    if hostname.lower() == "localhost":
        raise ValueError("localhost not allowed.")
    try:
        addr = ipaddress.ip_address(hostname)
        for net in _PRIVATE_RANGES:
            if addr in net:
                raise ValueError(f"Private/loopback address {hostname!r} not allowed.")
    except ValueError as exc:
        if "not allowed" in str(exc):
            raise
        # It's a domain name — pass through


async def _ensure_page():
    """Return the current page, recreating it if closed/crashed."""
    global _page
    if _page is None or _page.is_closed():
        if _browser is None:
            raise RuntimeError("Browser is not initialized. Playwright may not be installed.")
        _page = await _browser.new_page()
        _page.set_default_timeout(BROWSER_TIMEOUT)
    return _page


async def browser_init() -> None:
    """Start Playwright and launch Chromium. Non-fatal if Playwright is not installed."""
    global _playwright, _browser, _page
    async with _lock:
        if _browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True)
            _page = await _browser.new_page()
            _page.set_default_timeout(BROWSER_TIMEOUT)
            logger.info("Browser initialized (Chromium headless)")
        except Exception as e:
            logger.warning("browser_init: failed to start browser — %s", e)
            _playwright = None
            _browser = None
            _page = None


async def browser_close() -> None:
    """Shut down browser and Playwright."""
    global _playwright, _browser, _page
    try:
        if _page and not _page.is_closed():
            await _page.close()
        if _browser:
            await _browser.close()
        if _playwright:
            await _playwright.stop()
    except Exception as e:
        logger.warning("browser_close: error during shutdown — %s", e)
    finally:
        _playwright = None
        _browser = None
        _page = None


async def browser_navigate(url: str) -> str:
    """Navigate to a URL. Returns HTTP status and page title."""
    try:
        check_url(url)  # defense-in-depth: enforce at the function boundary too
        page = await _ensure_page()
        response = await page.goto(url)
        status = response.status if response else "unknown"
        title = await page.title()
        return f"Navigated to {url} — status: {status}, title: {title!r}"
    except Exception as e:
        return f"ERROR: browser_navigate failed: {e}"


async def browser_click(selector: str) -> str:
    """Click the element matching the CSS selector."""
    try:
        page = await _ensure_page()
        await page.click(selector)
        return f"Clicked element matching {selector!r}"
    except Exception as e:
        return f"ERROR: browser_click failed: {e}"


async def browser_fill(selector: str, text: str) -> str:
    """Fill a form field matching the CSS selector with the given text."""
    try:
        page = await _ensure_page()
        await page.fill(selector, text)
        return f"Filled {selector!r} with text ({len(text)} chars)"
    except Exception as e:
        return f"ERROR: browser_fill failed: {e}"


async def browser_extract_text() -> str:
    """Return the visible text content of the current page (capped at 20 KB)."""
    try:
        page = await _ensure_page()
        text = await page.inner_text("body")
        cap = 20 * 1024
        if len(text) > cap:
            text = text[:cap] + f"\n[... truncated at {cap} chars ...]"
        return text
    except Exception as e:
        return f"ERROR: browser_extract_text failed: {e}"


async def browser_screenshot(filename_hint: str = "") -> str:
    """Save a PNG screenshot inside the workspace screenshots/ directory and return the path."""
    try:
        page = await _ensure_page()
        # Sanitize hint to alphanumerics and hyphens only
        safe_hint = re.sub(r"[^a-zA-Z0-9_-]", "", filename_hint)[:40]
        suffix = f"_{safe_hint}" if safe_hint else ""
        # Save inside workspace so read_file can access it
        workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        screenshots_dir = os.path.join(workspace, "claude-agent", "screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="screenshot", suffix=f"{suffix}.png", dir=screenshots_dir)
        os.close(fd)
        await page.screenshot(path=path)
        return f"Screenshot saved to {path}"
    except Exception as e:
        return f"ERROR: browser_screenshot failed: {e}"
