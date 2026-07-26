"""Curry Leaves's web_fetch — fetch a URL and return clean main content as markdown.

Improves on smart-loop's regex stripper in two ways:
  1. Readability extraction (trafilatura) drops nav / ads / footers / cookie banners
     and keeps only the article body, rendered as markdown.
  2. For JavaScript-rendered pages where the static HTML is too thin, it re-fetches
     with a headless Playwright browser — when one is installed; otherwise it just
     uses whatever the static fetch returned (graceful degradation).

Pure-ish: the HTTP/render steps are isolated so extraction stays unit-testable.
"""
from __future__ import annotations

import html as _html
import re

import httpx
from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

_UA = "Mozilla/5.0 (compatible; curry-leaves/2.0; +https://curry-leaves.local)"
MAX_FETCH_CHARS = 12_000
_THIN = 500  # static extraction under this many chars → try rendering the JS
_RENDER_TIMEOUT = 15_000  # ms — page.goto() wait cap for the headless-render fallback


def _crude_strip(s: str) -> str:
    """Dependency-free last resort: drop script/style, strip tags, unescape."""
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"[ \t]*\n\s*\n\s*", "\n\n", _html.unescape(s)).strip()


def extract_content(page_html: str, url: str) -> str:
    """Readability extraction → markdown (main article only). Crude-strip on failure."""
    try:
        import trafilatura
        md = trafilatura.extract(
            page_html, url=url, output_format="markdown",
            include_links=True, include_tables=True, favor_recall=True,
        )
        if md and md.strip():
            return md.strip()
    except Exception:
        pass
    return _crude_strip(page_html)


async def _render(url: str) -> str | None:
    """Fully render a page with headless Chromium (Playwright). None if unavailable."""
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=_UA)
                # domcontentloaded, not networkidle: news/ad-heavy sites often never go
                # >=500ms fully quiet (live tickers, analytics beacons, ad refreshes), so
                # networkidle rode this call's timeout on nearly every article fetch. The
                # article body is present well before network activity settles.
                await page.goto(url, wait_until="domcontentloaded", timeout=_RENDER_TIMEOUT)
                return await page.content()
            finally:
                await browser.close()
    except Exception:
        return None


def _finish(text: str, ctx) -> ToolResult:
    """Truncate to MAX_FETCH_CHARS, offloading the full text to artifact:// like bash."""
    if len(text) <= MAX_FETCH_CHARS:
        return ToolResult(content=text)
    preview = text[:MAX_FETCH_CHARS]
    if getattr(ctx, "blobs", None) is not None:
        bid = ctx.blobs.put_text(text)
        return ToolResult(content=preview + f"\n... [truncated — full content at artifact://{bid}]")
    return ToolResult(content=preview + "\n... [truncated]")


class WebFetchTool:
    name = "web_fetch"
    risk = "network"
    description = (
        "Fetch ONE web page and return its main content as clean markdown — navigation, "
        "ads, and boilerplate removed. Auto-falls-back to headless rendering for "
        "JS-heavy pages when needed. This is READ-ONLY (no clicking/filling/screenshot) "
        "— use `browser` instead if you need to interact with the page (click a link, "
        "submit a form, log in, paginate) or need a screenshot; for a plain read this "
        "tool is simpler and doesn't need Playwright installed."
    )

    class Args(BaseModel):
        url: str = Field(description="The http(s) URL to fetch.")
        render: bool = Field(
            default=False,
            description="Force headless-browser rendering for JS-heavy pages. Off by "
                        "default; auto-enabled when the static page has little content.",
        )

    schema = Args
    # Static fetch is capped at 20s (httpx timeout below); the render fallback adds up
    # to _RENDER_TIMEOUT on top. 45s covers both stages with room to spare, well under
    # the app-wide 120s RunConfig.tool_timeout backstop (agent_engine.build_runner).
    timeout = 45.0

    async def run(self, args: "WebFetchTool.Args", ctx, signal) -> ToolResult:
        if not args.url.startswith(("http://", "https://")):
            return ToolResult(content=f"Not an http(s) URL: {args.url}", is_error=True)

        page_html: str | None = None
        fetch_err: str | None = None
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                         headers={"User-Agent": _UA}) as c:
                r = await c.get(args.url)
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                if "html" not in ctype and "xml" not in ctype:
                    # Non-HTML (json / plain text / etc.) — return verbatim.
                    return _finish(r.text, ctx)
                page_html = r.text
        except httpx.HTTPError as e:
            fetch_err = str(e)

        content = extract_content(page_html, args.url) if page_html else ""

        # JS-rendered page (or explicit request, or a failed static fetch) → try a browser.
        if args.render or len(content) < _THIN or fetch_err:
            rendered = await _render(args.url)
            if rendered:
                content = extract_content(rendered, args.url) or content

        if not content:
            return ToolResult(content=
                f"Failed to fetch {args.url}: {fetch_err}" if fetch_err
                else f"No readable content at {args.url}.",
                is_error=True,
            )
        return _finish(content, ctx)
