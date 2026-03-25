"""
screenshot.py — take a screenshot of a URL and save it to the feed.

Usage:
    python claude-agent/screenshot.py <url> [name]

Arguments:
    url   — page to screenshot
    name  — optional filename stem (default: timestamp). Saved as
            claude-agent/static/screenshots/<name>.png

The saved path is printed to stdout so the caller knows where it landed.
Requires: pip install playwright && python -m playwright install chromium
"""

import asyncio
import os
import sys
from datetime import datetime


async def _shot(url: str, name: str) -> str:
    from playwright.async_api import async_playwright

    out_dir = os.path.join(os.path.dirname(__file__), "static", "screenshots")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{name}.png")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        await page.screenshot(path=out_path, full_page=False)
        await browser.close()

    # Return the workspace-relative path so claude_session.py emits the image event
    rel = os.path.relpath(out_path, os.path.join(os.path.dirname(__file__), ".."))
    return rel.replace("\\", "/")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python claude-agent/screenshot.py <url> [name]", file=sys.stderr)
        sys.exit(1)

    url  = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%H%M%S")
    # Sanitise name — keep only safe characters
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)

    path = asyncio.run(_shot(url, name))
    print(f"screenshot saved: {path}")


if __name__ == "__main__":
    main()
