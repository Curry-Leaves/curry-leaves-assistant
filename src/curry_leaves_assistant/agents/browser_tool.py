"""browser — headless web automation via Playwright.

Copied from smart-loop's browser tool and adapted to curry-leaves imports.
Unlike `web_fetch` (raw HTML, no JavaScript), this drives a real browser:
it RENDERS pages, and can click, fill forms, and screenshot.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from curry_leaves.core.tools import ToolResult
from curry_leaves.providers.base import Context

MAX_BROWSER_CHARS = 15_000
_NAV_TIMEOUT = 30_000
_ACT_TIMEOUT = 10_000


class BrowserSession:
    def __init__(self, headless: bool = True) -> None:
        self._headless = headless
        self._pw = None
        self._browser = None
        self._page = None

    async def _ensure(self):
        if self._page is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            self._page = await self._browser.new_page()
        return self._page

    async def goto(self, url: str) -> str:
        page = await self._ensure()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        return f"Loaded {page.url} (status {resp.status if resp else '?'}) — title: {await page.title()!r}"

    async def text(self, selector: str = "") -> str:
        page = await self._ensure()
        if selector:
            await page.wait_for_selector(selector, timeout=_ACT_TIMEOUT)
            return await page.inner_text(selector)
        return await page.inner_text("body")

    async def click(self, selector: str) -> str:
        page = await self._ensure()
        await page.click(selector, timeout=_ACT_TIMEOUT)
        return f"Clicked {selector!r} — now at {page.url}"

    async def fill(self, selector: str, value: str) -> str:
        page = await self._ensure()
        await page.fill(selector, value, timeout=_ACT_TIMEOUT)
        return f"Filled {selector!r}"

    async def screenshot(self, path: str) -> str:
        page = await self._ensure()
        await page.screenshot(path=path, full_page=True)
        return f"Screenshot saved to {path}"

    async def stop(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        finally:
            self._pw = self._browser = self._page = None


class BrowserTool:
    name = "browser"
    risk = "network"
    description = (
        "Drive a headless browser when you need to INTERACT with a page, not just read "
        "it — click a link, fill/submit a form, log in, paginate, or take a screenshot. "
        "For a plain 'read this page's content' task, use web_fetch instead — it's "
        "simpler, needs no Playwright install, and already renders JS pages when the "
        "static fetch comes back thin.\n\n"
        "Actions: 'goto' (url), 'text' (visible text — pass a `selector` to extract "
        "just that region), 'click' (selector), 'fill' (selector + value), 'screenshot' "
        "(path). State (the loaded page) persists across calls within this run."
    )

    class Args(BaseModel):
        action: Literal["goto", "text", "click", "fill", "screenshot"] = Field(description="What to do.")
        url: str = Field(default="", description="For 'goto': the URL.")
        selector: str = Field(default="", description="CSS selector.")
        value: str = Field(default="", description="For 'fill': the text to enter.")
        path: str = Field(default="screenshot.png", description="For 'screenshot': output file.")

    schema = Args
    # Outer bound above the largest internal Playwright timeout (_NAV_TIMEOUT=30s) plus
    # headroom for a cold Chromium launch on first use — internal timeouts are the normal
    # exit path; this is the backstop if the browser process itself wedges.
    timeout = 45.0

    def __init__(self) -> None:
        self._session: BrowserSession | None = None

    async def run(self, args: "BrowserTool.Args", ctx: Context, signal) -> ToolResult:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return ToolResult(content=
                "browser needs Playwright: `pip install playwright && playwright install chromium`.",
                is_error=True,
            )
        if self._session is None:
            from curry_leaves_assistant.stores import tools_store  # lazy to avoid a circular import
            headless = tools_store.get("browser").get("config", {}).get("headless", True)
            self._session = BrowserSession(headless=headless)
        try:
            if args.action == "goto":
                out = await self._session.goto(args.url)
            elif args.action == "text":
                out = await self._session.text(args.selector)
            elif args.action == "click":
                out = await self._session.click(args.selector)
            elif args.action == "fill":
                out = await self._session.fill(args.selector, args.value)
            else:
                out = await self._session.screenshot(args.path)
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                return ToolResult(content=
                    "browser needs its Chromium binary: run `playwright install chromium` "
                    "(the pip package is present but the browser was never downloaded).",
                    is_error=True,
                )
            return ToolResult(content=f"browser error: {type(e).__name__}: {e}", is_error=True)

        if len(out) > MAX_BROWSER_CHARS:
            return ToolResult(content=out[:MAX_BROWSER_CHARS] + "\n... [truncated]")
        return ToolResult(content=out or "(no output)")

    async def shutdown(self) -> None:
        if self._session is not None:
            await self._session.stop()
