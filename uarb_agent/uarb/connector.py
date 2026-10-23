"""Playwright connector for the UARB public documents database (FileMaker WebDirect).

    connector = Connector()
    async with connector.session() as s:
        await s.open_matter("M12205")
        meta, counts = await s.read_metadata(), await s.read_counts()
        rows = await s.list_documents(DocType.OTHER_DOCUMENTS)
        files = await s.download(rows[:2], Path("data/tmp"))

Any exception leaving `session()` saves a screenshot and the page HTML to data/traces/<trace_id>/ and
comes out as a AgentError (Playwright failures become SiteUnavailable) carrying that trace_id."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import structlog
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from uarb_agent.config import Settings, get_settings
from uarb_agent.models import (
    DocRow,
    DocType,
    FileResult,
    LayoutChanged,
    MatterMetadata,
    SiteUnavailable,
    UnexpectedModal,
    normalize_matter,
)
from uarb_agent.uarb.browser import Browser
from uarb_agent.uarb.errors import as_agent_error, clean_modal_text, modal_error
from uarb_agent.uarb.parse import parse_counts, parse_found_count, parse_metadata, parse_row
from uarb_agent.uarb.settle import settle, wait_modal_gone

log = structlog.get_logger(__name__)

MATTER_BOX = "div.fm-textarea:has(div.placeholder:text-is('eg M01234'))"
SCROLLER = ".v-grid-scroller-vertical"
MAX_SCROLLS = 200
SCROLL_PAUSE_MS = 400

# Visible text boxes above the tab bar: the header labels and their values.
HEADER_JS = r"""() => {
  const tabRe = /^\s*(Exhibits|Key Documents|Other Documents|Transcripts|Recordings)\s*-\s*\d+\s*$/;
  const tabs = [...document.querySelectorAll('button')].filter(b => tabRe.test(b.innerText));
  const limit = tabs.length ? Math.min(...tabs.map(b => b.getBoundingClientRect().top)) : Infinity;
  const out = [], seen = new Set();
  document.querySelectorAll('.v-label .fm-text-character, .fm-textarea').forEach(e => {
    if (e.closest('tr, .v-window, button')) return;
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.top >= limit) return;
    const t = e.innerText.trim();
    const key = `${t}@${Math.round(r.x)},${Math.round(r.y)}`;
    if (t && !seen.has(key)) { seen.add(key); out.push({t, x: r.x, y: r.y, w: r.width, h: r.height}); }
  });
  return out;
}"""

# Rendered grid rows (the grid is virtualised: only ~12 rows exist at a time) with row-relative boxes.
GRID_JS = r"""() => {
  const sc = document.querySelector('.v-grid-scroller-vertical');
  const s = sc && sc.getBoundingClientRect();
  const rows = [...document.querySelectorAll('tr.v-grid-row-has-data')].map((tr, i) => {
    const R = tr.getBoundingClientRect();
    const boxes = [...tr.querySelectorAll('.fm-textarea')].map(e => {
      const r = e.getBoundingClientRect();
      return {t: e.innerText.trim(), x: r.x - R.x, y: r.y - R.y, w: r.width, h: r.height};
    }).filter(b => b.t);
    return {i, top: R.top, bottom: R.bottom, boxes};
  }).sort((a, b) => a.top - b.top);
  return {
    rows,
    view: s && s.height > 0 ? {top: s.top, bottom: s.bottom} : null,
    at_bottom: !sc || sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 2,
  };
}"""

MARK_ROW_JS = r"""(i) => {
  document.querySelectorAll('tr[data-agent-target]').forEach(t => t.removeAttribute('data-agent-target'));
  document.querySelectorAll('tr.v-grid-row-has-data')[i].setAttribute('data-agent-target', '1');
}"""

TAB_READY_JS = r"""(before) => {
  const t = document.body.innerText;
  return !!document.querySelector('.v-window') || (t !== before && t.includes('GO GET IT'));
}"""


def new_trace_id() -> str:
    return f"{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._()-]+", "_", name).strip("._")[:120]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Connector:
    """Owns the shared browser; hands out one isolated session per request."""

    def __init__(self, settings: Settings | None = None, browser: Browser | None = None):
        self.settings = settings or get_settings()
        self.browser = browser or Browser(
            concurrency=self.settings.uarb_concurrency, min_gap_s=self.settings.uarb_session_gap_s
        )

    @asynccontextmanager
    async def session(self, trace_id: str | None = None) -> AsyncIterator[UarbSession]:
        trace_id = trace_id or new_trace_id()
        async with self.browser.page() as page:
            s = UarbSession(page, url=self.settings.uarb_url, trace_dir=self.settings.traces_dir / trace_id)
            try:
                yield s
            except Exception as exc:
                await s.save_trace("error", exc)
                err = as_agent_error(exc)
                if err is None:
                    raise
                err.trace_id = trace_id
                if err is exc:
                    raise
                raise err from exc

    async def close(self) -> None:
        await self.browser.close()


class UarbSession:
    """One WebDirect session in one browser context. Methods must be called in order: open_matter first."""

    def __init__(self, page: Page, *, url: str, trace_dir: Path):
        self.page = page
        self.url = url
        self.trace_dir = trace_dir
        self.trace_id = trace_dir.name
        self.matter: str | None = None
        self.counts: dict[DocType, int] | None = None
        self.timings: dict[str, float] = {}
        self._tab: DocType | None = None

    @contextmanager
    def _stage(self, name: str) -> Iterator[None]:
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.timings[name] = round(self.timings.get(name, 0.0) + time.monotonic() - t0, 3)

    # --- navigation --------------------------------------------------------------------------------

    async def open_matter(self, matter: str) -> None:
        """Search for `matter` from the home page. Raises InvalidMatterNumber before touching the site,
        MatterNotFound on the site's no-records dialog, SiteUnavailable if nothing answers."""
        self.matter = matter = normalize_matter(matter)
        page = self.page
        with self._stage("open"):
            try:
                await page.goto(self.url, wait_until="networkidle", timeout=45_000)
            except PlaywrightError as e:
                raise SiteUnavailable(f"home page did not load: {str(e).splitlines()[0]}") from e
            box = page.locator(MATTER_BOX)
            try:
                await box.wait_for(timeout=30_000)
            except PlaywrightTimeout as e:
                raise LayoutChanged("matter search box not found on the home page") from e

            await self._type_matter(box, matter)
            await (await self._search_button(box)).click()
            try:
                await page.wait_for_function(
                    "document.body.innerText.includes('Exhibits - ') || !!document.querySelector('.v-window')",
                    timeout=15_000,
                )
            except PlaywrightTimeout as e:
                raise SiteUnavailable("no response to the matter search within 15 s") from e
            if text := await self.read_modal():
                raise modal_error(text, matter=matter)
            await settle(page)
        self._tab = None

    async def _type_matter(self, box: Locator, matter: str) -> None:
        # The box is a div that turns contenteditable after a server round trip. One insert_text is one
        # input event; per-key typing gets overwritten by server echoes (the server once received '05').
        typed = ""
        for attempt in range(2):
            await box.click()
            await self.page.wait_for_function(
                "document.activeElement && document.activeElement.isContentEditable", timeout=10_000
            )
            await self.page.wait_for_timeout(300)
            if attempt:
                await self.page.keyboard.press("ControlOrMeta+A")
                await self.page.keyboard.press("Backspace")
            await self.page.keyboard.insert_text(matter)
            await self.page.wait_for_timeout(500)
            typed = (await self.page.evaluate("document.activeElement ? document.activeElement.innerText : ''")).strip()
            if typed.upper() == matter:
                return
            log.warning("uarb.matter_box.mismatch", typed=typed, expected=matter, attempt=attempt)
        raise LayoutChanged(f"matter box reads {typed!r} after typing {matter}")

    async def _search_button(self, box: Locator) -> Locator:
        """There are three 'Search' buttons; the right one shares the matter box's row, to its right."""
        bb = await box.bounding_box()
        buttons = self.page.locator("button:has-text('Search')")
        if bb:
            mid = bb["y"] + bb["height"] / 2
            for i in range(await buttons.count()):
                bx = await buttons.nth(i).bounding_box()
                if bx and abs(bx["y"] + bx["height"] / 2 - mid) < 20 and bx["x"] > bb["x"]:
                    return buttons.nth(i)
        raise LayoutChanged("no Search button beside the matter box")

    async def read_modal(self) -> str | None:
        """Text of the open WebDirect dialog, or None."""
        window = self.page.locator(".v-window")
        if not await window.count():
            return None
        return (await window.first.inner_text()).strip()

    async def _dismiss_modal(self) -> None:
        window = self.page.locator(".v-window").first
        button = window.get_by_role("button", name=re.compile(r"^(OK|Close)$", re.I))
        if await button.count():
            await button.first.click()
        if not await wait_modal_gone(self.page):
            raise LayoutChanged("a dialog would not close; the session cannot continue")

    # --- reading -----------------------------------------------------------------------------------

    async def read_metadata(self) -> MatterMetadata:
        with self._stage("metadata"):
            meta = parse_metadata(await self.page.evaluate(HEADER_JS))
        if self.matter and meta.matter_no != self.matter:
            raise LayoutChanged(f"header shows {meta.matter_no}, expected {self.matter}")
        return meta

    async def read_counts(self) -> dict[DocType, int]:
        with self._stage("counts"):
            self.counts = parse_counts(await self.page.locator("button").all_inner_texts())
        return self.counts

    async def list_documents(self, tab: DocType, count: int | None = None) -> list[DocRow]:
        """Every row of `tab` in site order. A tab whose count is 0 is never clicked."""
        tab = DocType(tab)
        if count is None:
            count = (self.counts or await self.read_counts())[tab]
        if count == 0:
            return []
        with self._stage("list"):
            await self._open_tab(tab)
            target = parse_found_count(await settle(self.page)) or count
            rows = await self._collect_rows(tab, target)
        if not rows:
            raise LayoutChanged(f"{tab} shows {count} documents but no rows parsed")
        if len(rows) != target:
            log.warning("uarb.list.short", tab=str(tab), collected=len(rows), expected=target)
        return rows

    async def _open_tab(self, tab: DocType) -> None:
        if self._tab == tab:
            return
        before = await self.page.evaluate("document.body.innerText")
        button = self.page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(tab)}\s*-\s*\d+\s*$"))
        if not await button.count():
            raise LayoutChanged(f"no tab button for {tab}")
        await button.first.click()
        try:
            await self.page.wait_for_function(TAB_READY_JS, arg=before, timeout=15_000)
        except PlaywrightTimeout as e:
            raise SiteUnavailable(f"the {tab} tab did not open within 15 s") from e
        if text := await self.read_modal():
            await self._dismiss_modal()
            raise modal_error(text, matter=self.matter or "", tab=tab)
        self._tab = tab

    async def _grid(self) -> dict:
        return await self.page.evaluate(GRID_JS)

    async def _scroll(self, js: str, arg: float | None = None) -> None:
        scroller = self.page.locator(SCROLLER)
        if await scroller.count():
            await scroller.first.evaluate(js, arg)
            await self.page.wait_for_timeout(SCROLL_PAUSE_MS)

    async def _collect_rows(self, tab: DocType, target: int) -> list[DocRow]:
        rows: dict[str, DocRow] = {}
        stalls = 0
        for _ in range(MAX_SCROLLS):
            before = len(rows)
            grid = await self._grid()
            for r in grid["rows"]:
                if row := parse_row(r["boxes"], tab):  # a row still rendering has no text yet
                    rows.setdefault(row.doc_no, row)
            if len(rows) >= target or (grid["at_bottom"] and len(rows) == before):
                break
            stalls = stalls + 1 if len(rows) == before else 0
            if stalls >= 3:
                break
            await self._scroll("e => e.scrollBy(0, e.clientHeight * 0.8)")
        await self._scroll("e => { e.scrollTop = 0; }")
        return list(rows.values())

    # --- downloading -------------------------------------------------------------------------------

    async def download(self, rows: list[DocRow], dest: Path) -> list[FileResult]:
        """Fetch each row through its GO GET IT dialog into `dest`. One FileResult per file saved (a
        dialog can offer several), or one failed FileResult per row that could not be fetched.
        A failed row does not stop the others; a dialog that will not close does."""
        await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
        results: list[FileResult] = []
        with self._stage("download"):
            for row in rows:
                results.extend(await self._download_row(row, dest))
        return results

    async def _download_row(self, row: DocRow, dest: Path) -> list[FileResult]:
        error = ""
        for attempt in range(2):
            try:
                return await self._try_download(row, dest)
            except UnexpectedModal as e:  # the site said no; asking again will not change that
                error = str(e)
                break
            except (PlaywrightError, LayoutChanged) as e:
                error = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
                log.warning("uarb.download.retry", doc_no=row.doc_no, attempt=attempt, error=error)
                await self.save_trace(f"download_{safe_filename(row.doc_no)}_{attempt}", e)
                if await self.read_modal() is not None:
                    await self._dismiss_modal()
        return [FileResult(doc_no=row.doc_no, title=row.title, ok=False, error=error)]

    async def _try_download(self, row: DocRow, dest: Path) -> list[FileResult]:
        await self._open_tab(row.tab)
        await (await self._go_get_it(row)).click()
        window = self.page.locator(".v-window").first
        await window.wait_for(timeout=10_000)
        header = (await window.locator(".v-window-header").inner_text()).strip()
        if "download" not in header.lower():
            text = await window.inner_text()
            await self._dismiss_modal()
            raise modal_error(text, matter=self.matter or "", tab=row.tab)

        buttons = window.locator(".fm-download-button")
        try:
            await buttons.first.wait_for(timeout=5_000)
        except PlaywrightTimeout:
            text = clean_modal_text(await window.inner_text())
            await self._dismiss_modal()
            raise UnexpectedModal(text) from None
        files = [await self._save(buttons.nth(i), row, dest) for i in range(await buttons.count())]
        await self._dismiss_modal()
        return files

    async def _go_get_it(self, row: DocRow) -> Locator:
        """Bring `row` fully into the grid's viewport and return its GO GET IT button."""
        await self._scroll("e => { e.scrollTop = 0; }")
        for _ in range(MAX_SCROLLS):
            grid = await self._grid()
            match = next(
                (r for r in grid["rows"] if (p := parse_row(r["boxes"], row.tab)) and p.doc_no == row.doc_no), None
            )
            view = grid["view"]
            if match is None:
                if grid["at_bottom"]:
                    raise LayoutChanged(f"row {row.doc_no} not found in the {row.tab} grid")
                await self._scroll("e => e.scrollBy(0, e.clientHeight * 0.8)")
            elif view and match["top"] < view["top"] - 1:
                await self._scroll("(e, d) => e.scrollBy(0, d)", match["top"] - view["top"])
            elif view and match["bottom"] > view["bottom"] + 1:
                await self._scroll("(e, d) => e.scrollBy(0, d)", match["bottom"] - view["bottom"])
            else:
                await self.page.evaluate(MARK_ROW_JS, match["i"])
                return self.page.locator("tr[data-agent-target]").locator("button", has_text="GO GET IT")
        raise LayoutChanged(f"could not scroll row {row.doc_no} into view")

    async def _save(self, button: Locator, row: DocRow, dest: Path) -> FileResult:
        async with self.page.expect_download(timeout=20_000) as info:
            await button.click()
        download = await info.value
        name = safe_filename(download.suggested_filename) or safe_filename(f"{row.doc_no}{row.ext or ''}")
        part = dest / f"{name}.part"
        await download.save_as(part)
        failure = await download.failure()
        size = (await asyncio.to_thread(part.stat)).st_size if not failure else 0
        if failure or size == 0:
            await asyncio.to_thread(part.unlink, missing_ok=True)
            return FileResult(doc_no=row.doc_no, title=row.title, error=failure or "empty file")
        digest = await asyncio.to_thread(_sha256, part)
        final = await asyncio.to_thread(part.replace, dest / name)
        log.info("uarb.download.ok", doc_no=row.doc_no, file=name, size=size)
        return FileResult(doc_no=row.doc_no, title=row.title, path=final, size=size, sha256=digest, ok=True)

    # --- tracing -----------------------------------------------------------------------------------

    async def save_trace(self, label: str, exc: BaseException | None = None) -> Path | None:
        """Screenshot + HTML (+ error text) into trace_dir. Never raises: tracing must not mask the error."""
        try:
            await asyncio.to_thread(self.trace_dir.mkdir, parents=True, exist_ok=True)
            await self.page.screenshot(path=self.trace_dir / f"{label}.png", full_page=True, timeout=10_000)
            html = await self.page.content()
            await asyncio.to_thread((self.trace_dir / f"{label}.html").write_text, html)
            if exc is not None:
                note = f"{type(exc).__name__}: {exc}\nmatter={self.matter} tab={self._tab}\n"
                await asyncio.to_thread((self.trace_dir / f"{label}.txt").write_text, note)
            log.info("uarb.trace.saved", dir=str(self.trace_dir), label=label)
            return self.trace_dir
        except Exception as e:  # page may be gone
            log.warning("uarb.trace.failed", error=repr(e))
            return None
