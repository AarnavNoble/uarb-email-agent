"""One shared Chromium for the process; every request gets its own fresh context.

The UARB server licenses a limited number of WebDirect connections and a closed context does not
log out (the session lingers until its heartbeat lapses), so sessions are capped and spaced."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import structlog
from playwright.async_api import Browser as PWBrowser
from playwright.async_api import Page, Playwright, async_playwright

log = structlog.get_logger(__name__)

VIEWPORT = {"width": 1400, "height": 1000}


class Browser:
    def __init__(self, *, concurrency: int = 2, min_gap_s: float = 2.0, headless: bool = True):
        self._sem = asyncio.Semaphore(concurrency)
        self._min_gap_s = min_gap_s
        self._headless = headless
        self._launch_lock = asyncio.Lock()
        self._gap_lock = asyncio.Lock()
        self._last_start = float("-inf")
        self._pw: Playwright | None = None
        self._browser: PWBrowser | None = None

    async def _ensure(self) -> PWBrowser:
        async with self._launch_lock:
            if self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=self._headless)
                log.info("uarb.browser.launched")
            return self._browser

    async def _wait_gap(self) -> None:
        async with self._gap_lock:
            wait = self._last_start + self._min_gap_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_start = time.monotonic()

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        """A page in a brand-new context; the context is closed however the caller exits."""
        async with self._sem:
            await self._wait_gap()
            browser = await self._ensure()
            try:
                ctx = await browser.new_context(viewport=VIEWPORT, accept_downloads=True)
            except Exception as e:
                # A dead driver or browser fails here on every call (is_connected() can still say True), so
                # start a fresh Playwright once instead of failing every request for the rest of the run.
                log.warning("uarb.browser.relaunch", error=repr(e))
                async with self._launch_lock:
                    if self._browser is browser:  # a concurrent caller may already have relaunched
                        await self.close()
                ctx = await (await self._ensure()).new_context(viewport=VIEWPORT, accept_downloads=True)
            ctx.set_default_timeout(15_000)
            try:
                yield await ctx.new_page()
            finally:
                with suppress(Exception):
                    await ctx.close()

    async def close(self) -> None:
        with suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        with suppress(Exception):
            if self._pw is not None:
                await self._pw.stop()
        self._browser = self._pw = None
