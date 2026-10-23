"""Waiting helpers. WebDirect pushes updates over a websocket and never changes the URL, so neither
navigation events nor networkidle say when a screen is done; DOM text stability does."""

from __future__ import annotations

import time

from playwright.async_api import Page

CURTAIN = ".v-window-modalitycurtain"


async def settle(page: Page, *, quiet_ms: int = 1200, timeout_s: float = 30.0, poll_ms: int = 200) -> str:
    """Wait until document.body.innerText has not changed for `quiet_ms`; return that text.

    On timeout the last text seen is returned: a page that keeps ticking is still usable, and the
    callers validate what they parse from it anyway."""
    start = since = time.monotonic()
    last: str | None = None
    while True:
        text = await page.evaluate("document.body.innerText")
        now = time.monotonic()
        if text != last:
            last, since = text, now
        elif (now - since) * 1000 >= quiet_ms:
            return text
        if now - start >= timeout_s:
            return last
        await page.wait_for_timeout(poll_ms)


async def modal_open(page: Page) -> bool:
    return await page.locator(".v-window").count() > 0


async def wait_modal_gone(page: Page, *, timeout_ms: int = 10_000) -> bool:
    """True once no dialog and no click-blocking curtain remain."""
    try:
        await page.wait_for_function(
            f"!document.querySelector('.v-window') && !document.querySelector('{CURTAIN}')", timeout=timeout_ms
        )
    except Exception:
        return False
    return True
