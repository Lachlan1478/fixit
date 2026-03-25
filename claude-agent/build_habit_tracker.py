#!/usr/bin/env python3
"""
build_habit_tracker.py — Playwright script that drives the Claude Agent UI
to build a daily habit tracker web app.

Usage:
    python claude-agent/build_habit_tracker.py

Requirements:
    pip install playwright
    playwright install chromium

The Claude Agent server must be running at http://localhost:8007.
"""

import asyncio
import sys

from playwright.async_api import async_playwright

BASE_URL = "http://localhost:8007"

BUILD_PROMPT = """\
Create claude-agent/static/habits.html — a complete single-file habit tracker web app.

Requirements:
- Add habits by name (text input + button)
- Delete habits (× button per habit)
- Check off each habit for today (checkbox per habit, resets at midnight)
- Show a streak counter per habit (consecutive days completed)
- Persist all data in localStorage
- Dark theme: background #0f0f11, accent #7c6af7, text #e8e8f0, surface #1a1a1f
- Mobile-friendly, max-width 480px centered
- All HTML, CSS, and JS in one file

Write the complete file using the Write tool. Do not explain, just write the file.
"""


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # ── 1. Navigate ───────────────────────────────────────────────────
        print(f"[1/6] Navigating to {BASE_URL}…")
        await page.goto(BASE_URL)
        await page.wait_for_load_state("networkidle")

        # ── 2. Reset session ──────────────────────────────────────────────
        print("[2/6] Resetting session…")
        await page.evaluate("""
            fetch('/reset_memory', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({agent_id: 'default'}),
            })
        """)
        await asyncio.sleep(1)

        # ── 3. Select Sonnet ──────────────────────────────────────────────
        print("[3/6] Selecting Sonnet model…")
        await page.click('button[data-model="sonnet"]')

        # ── 4. Fill and send build prompt ─────────────────────────────────
        print("[4/6] Sending build prompt…")
        await page.fill("#prompt", BUILD_PROMPT)
        await page.click("#send-btn")

        # ── 5. Wait for completion ────────────────────────────────────────
        print("[5/6] Waiting for Claude to finish (up to 180 s)…")
        try:
            await page.wait_for_selector(".feed-item.done", timeout=180_000)
            print("      ✓ Done event received")
        except Exception as exc:
            print(f"      ✗ Timeout waiting for done: {exc}")
            await page.screenshot(path="01_timeout.png")
            await browser.close()
            sys.exit(1)

        # ── 6. Verify habits.html exists ──────────────────────────────────
        print("[6/6] Verifying habits.html was created…")
        resp = await page.evaluate("""
            fetch('/file?path=claude-agent/static/habits.html')
                .then(r => r.json())
        """)
        if resp.get("exists"):
            print("      ✓ habits.html exists!")
        else:
            print("      ✗ habits.html not found — Claude may not have written the file")
            await page.screenshot(path="01_no_file.png")
            await browser.close()
            sys.exit(1)

        await page.screenshot(path="01_build_complete.png")
        print("      Screenshot: 01_build_complete.png")

        await browser.close()
        print(f"\nBuild complete. App: {BASE_URL}/static/habits.html")


if __name__ == "__main__":
    asyncio.run(main())
