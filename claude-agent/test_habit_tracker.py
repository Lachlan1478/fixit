#!/usr/bin/env python3
"""
test_habit_tracker.py — Playwright tests for the habit tracker app.

Usage:
    python claude-agent/test_habit_tracker.py

The Claude Agent server must be running at http://localhost:8007 and
habits.html must exist (run build_habit_tracker.py first).

Tests use flexible selectors since the exact DOM depends on what Claude
generates, but they rely on common patterns (text input, checkboxes, buttons).
"""

import asyncio
import re
import sys

from playwright.async_api import async_playwright, expect

BASE_URL = "http://localhost:8007/static/habits.html"

_passed = 0
_failed = 0
_errors: list[str] = []


async def run_test(name: str, coro) -> None:
    global _passed, _failed
    try:
        await coro
        print(f"  ✓ {name}")
        _passed += 1
    except Exception as exc:
        print(f"  ✗ {name}: {exc}")
        _failed += 1
        _errors.append(f"{name}: {exc}")


# ── Helpers ────────────────────────────────────────────────────────────────────

async def add_habit(page, name: str) -> None:
    """Type a habit name and submit via Enter or Add button."""
    inp = page.locator("input[type='text'], input[placeholder]").first
    await inp.fill(name)
    # Try common button labels; fall back to Enter
    for label in ("Add", "add", "+", "Add Habit", "➕"):
        btn = page.locator(f"button:has-text('{label}')").first
        if await btn.count() > 0:
            await btn.click()
            return
    await inp.press("Enter")


async def click_delete_for(page, habit_name: str) -> None:
    """Click the delete button in the row that contains `habit_name`."""
    # Look for a button near the text — try common delete symbols
    for selector in [
        f"li:has-text('{habit_name}') button",
        f"div:has-text('{habit_name}') button",
        f"tr:has-text('{habit_name}') button",
    ]:
        btns = page.locator(selector)
        count = await btns.count()
        for i in range(count):
            btn = btns.nth(i)
            text = (await btn.inner_text()).strip()
            if text in ("×", "✕", "✗", "x", "X", "Delete", "Remove", "🗑", "🗑️", "delete"):
                await btn.click()
                return
    # fallback: click any button whose aria-label suggests deletion
    btn = page.locator(f"button[aria-label*='delete' i], button[aria-label*='remove' i]").first
    if await btn.count() > 0:
        await btn.click()


# ── Tests ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print(f"Running habit tracker tests against {BASE_URL}\n")

        # ── T1: Page loads ─────────────────────────────────────────────────
        async def t1() -> None:
            await page.goto(BASE_URL)
            await page.wait_for_load_state("networkidle")
            body = await page.locator("body").inner_text()
            assert body.strip(), "Page body is empty"
            inp = page.locator("input[type='text'], input[placeholder]").first
            await expect(inp).to_be_visible(timeout=5000)

        await run_test("Page loads with input field", t1())

        # ── T2: Add habit ──────────────────────────────────────────────────
        async def t2() -> None:
            await page.evaluate("localStorage.clear()")
            await page.reload()
            await page.wait_for_load_state("networkidle")
            await add_habit(page, "Exercise")
            await expect(page.locator("text=Exercise")).to_be_visible(timeout=5000)

        await run_test("Add habit — appears in list", t2())

        # ── T3: Check off habit ────────────────────────────────────────────
        async def t3() -> None:
            checkbox = page.locator("input[type='checkbox']").first
            await checkbox.check()
            await expect(checkbox).to_be_checked()

        await run_test("Check off habit — checkbox checked", t3())

        # ── T4: Persist after reload ───────────────────────────────────────
        async def t4() -> None:
            await page.reload()
            await page.wait_for_load_state("networkidle")
            await expect(page.locator("text=Exercise")).to_be_visible(timeout=5000)
            checkbox = page.locator("input[type='checkbox']").first
            await expect(checkbox).to_be_checked()

        await run_test("Data persists after page reload", t4())

        # ── T5: Add second habit ───────────────────────────────────────────
        async def t5() -> None:
            await add_habit(page, "Read")
            await expect(page.locator("text=Read")).to_be_visible(timeout=5000)
            count = await page.locator("input[type='checkbox']").count()
            assert count >= 2, f"Expected ≥ 2 habits, found {count}"

        await run_test("Second habit can be added", t5())

        # ── T6: Unchecked habit has zero / no streak ───────────────────────
        async def t6() -> None:
            # "Read" was just added and not checked — streak should be 0 or empty
            # Find the row/container for "Read"
            read_el = page.locator("text=Read").first
            parent = read_el.locator("xpath=ancestor::li[1] | ancestor::div[1]").last
            text = await parent.inner_text()
            # Strip the habit name itself so we only look at streak text
            streak_text = text.replace("Read", "").strip()
            # Accept: "0", "0 day", "—", "streak: 0", or no numeric > 0
            nums = re.findall(r"\d+", streak_text)
            non_zero = [int(n) for n in nums if int(n) > 0]
            assert not non_zero, f"Unchecked habit should have 0 streak, found: {streak_text!r}"

        await run_test("Unchecked habit shows zero / no streak", t6())

        # ── T7: Delete habit ───────────────────────────────────────────────
        async def t7() -> None:
            await click_delete_for(page, "Read")
            await expect(page.locator("text=Read")).to_be_hidden(timeout=5000)

        await run_test("Delete habit — removed from list", t7())

        # ── T8: Empty state ────────────────────────────────────────────────
        async def t8() -> None:
            # Delete all remaining habits
            for _ in range(20):
                checkboxes = page.locator("input[type='checkbox']")
                if await checkboxes.count() == 0:
                    break
                # Get any visible habit name to target delete
                labels = page.locator("label, li, .habit-name, [class*='habit']")
                if await labels.count() > 0:
                    name = (await labels.first.inner_text()).strip().split("\n")[0]
                    await click_delete_for(page, name)
                else:
                    # brute-force: click any delete-like button
                    btns = page.locator("button")
                    for i in range(await btns.count()):
                        t = (await btns.nth(i).inner_text()).strip()
                        if t in ("×", "✕", "✗", "x", "Delete", "Remove", "🗑"):
                            await btns.nth(i).click()
                            break
                await asyncio.sleep(0.2)

            final_count = await page.locator("input[type='checkbox']").count()
            assert final_count == 0, f"Expected 0 habits after deleting all, found {final_count}"

        await run_test("Empty state after deleting all habits", t8())

        await browser.close()

        # ── Summary ────────────────────────────────────────────────────────
        print(f"\nResults: {_passed} passed, {_failed} failed")
        if _errors:
            print("\nFailed tests:")
            for e in _errors:
                print(f"  • {e}")
        sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
