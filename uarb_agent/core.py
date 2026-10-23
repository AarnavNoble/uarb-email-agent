"""The one entry point every channel (email worker, CLI) calls: matter in, MatterReport out.

`Fetcher.fetch` reads metadata and the five counts, lists the requested tab, and downloads up to `limit`
documents starting at `offset`, going to the site only for what the cache cannot answer. Rows are taken newest
first by filing date: the site lists Other Documents that way, but Key Documents and Exhibits oldest first.
Rows not marked Public (Confidential, Board Only) are passed over and reported as skipped, because the site serves
a one-page confidentiality notice in their place; a downloaded notice is caught and skipped the same way. A row
whose download fails (the connector has already retried it once) is reported and the next row is taken instead.

After every fetch, off the event loop: each downloaded file goes into the full-text search index, and the report
gets a cited one-line `decision` when a Board Order or Decision for the matter is in the cache. Neither step can
fail a fetch; problems are logged.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import time
from contextlib import AsyncExitStack
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import pymupdf
import structlog

from uarb_agent.cache.store import Store
from uarb_agent.config import Settings, get_settings
from uarb_agent.extract.outcome import decision_for, render_line
from uarb_agent.models import (
    AgentError,
    DocRow,
    DocType,
    FileResult,
    LayoutChanged,
    MatterMetadata,
    MatterReport,
    SiteUnavailable,
    Skipped,
    normalize_matter,
)
from uarb_agent.pack.links import publish_link
from uarb_agent.pack.zipper import MB, PackResult, build_zip
from uarb_agent.search import SearchIndex, index_path
from uarb_agent.uarb.connector import new_trace_id

log = structlog.get_logger(__name__)

CONFIDENTIAL_REASON = "confidential; the public site only offers a notice in its place"
BOARD_ONLY_REASON = "Board only; the public site only offers a confidentiality notice in its place"
NOTICE_REASON = "the site served a confidentiality notice instead of the document"
# Downloads stop this many seconds into a fetch: no new one starts after it, and the one in flight is cut off at
# it. Exhibits can be 40+ MB each (about 40 s apiece on M12205), so ten of them would blow the worker's
# per-request timeout and lose the whole batch. Stopping early returns what was fetched; MORE resumes at the
# first row not finished.
DOWNLOAD_BUDGET_S = 80.0
# Recordings run 35-212 MB at about 1.1 MB/s (212 MB took 188 s), so the first one needs far longer than 80 s.
HEARING_BUDGET_S: dict[DocType | None, float] = {DocType.TRANSCRIPTS: 120.0, DocType.RECORDINGS: 240.0}
# Opening the matter, closing the session and tidying up around the downloads; added on top for a request timeout.
WRAP_UP_S = 40.0
# A real document is rarely this small; the site's confidentiality notice is a one-page PDF of about 100 KB.
NOTICE_MAX_BYTES = 512 * 1024


def request_timeout_s(doc_type: DocType | None) -> float:
    """A safe outer timeout for one `Fetcher.fetch` of `doc_type`: its download budget plus room to wrap up."""
    return HEARING_BUDGET_S.get(doc_type, DOWNLOAD_BUDGET_S) + WRAP_UP_S


class Session(Protocol):
    timings: dict[str, float]

    async def open_matter(self, matter: str) -> None: ...
    async def read_metadata(self) -> MatterMetadata: ...
    async def read_counts(self) -> dict[DocType, int]: ...
    async def list_documents(self, tab: DocType, count: int | None = None) -> list[DocRow]: ...
    async def download(self, rows: list[DocRow], dest: Path) -> list[FileResult]: ...


class SessionFactory(Protocol):
    """What Fetcher needs from uarb.Connector; `session` returns an async context manager yielding a Session."""

    def session(self, trace_id: str | None = None) -> Any: ...
    async def close(self) -> None: ...


def newest_first(rows: list[DocRow]) -> list[DocRow]:
    """By date, newest first; ties keep site order and undated rows go last."""
    return sorted(rows, key=lambda r: (r.date is not None, r.date or dt.date.min), reverse=True)


def not_public_reason(row: DocRow) -> str | None:
    """Why `row` cannot be fetched from the public site, or None. Blank security counts as public (hearing rows
    often have none); any other level than Public serves a confidentiality notice instead of the document."""
    level = " ".join((row.security or "").split())
    if level.lower() in ("", "public"):
        return None
    if level.lower() == "confidential":
        return CONFIDENTIAL_REASON
    if level.lower() == "board only":
        return BOARD_ONLY_REASON
    return f"marked {level!r}, not Public; the public site only offers a notice in its place"


def is_public(row: DocRow) -> bool:
    return not_public_reason(row) is None


def is_confidentiality_notice(path: Path) -> bool:
    """True for the site's stand-in PDF ('Document: 320036 ... CONFIDENTIALITY NOTICE ...'). Large files and
    anything pymupdf cannot open are taken at face value."""
    if path.suffix.lower() != ".pdf" or path.stat().st_size > NOTICE_MAX_BYTES:
        return False
    try:
        with pymupdf.open(path) as doc:
            head = doc[0].get_text()[:400] if doc.page_count else ""
    except Exception:
        return False
    return "CONFIDENTIALITY NOTICE" in " ".join(head.split())  # the notice's heading, in capitals


class _Site:
    """Opens one UARB session on first use and keeps it for the rest of a fetch."""

    def __init__(self, factory: SessionFactory, stack: AsyncExitStack, matter: str, trace_id: str):
        self.factory, self.stack, self.matter, self.trace_id = factory, stack, matter, trace_id
        self.session: Session | None = None
        self.metadata: MatterMetadata | None = None
        self.counts: dict[DocType, int] = {}

    async def get(self) -> Session:
        if self.session is None:
            s = await self.stack.enter_async_context(self.factory.session(self.trace_id))
            await s.open_matter(self.matter)
            self.metadata = await s.read_metadata()
            self.counts = await s.read_counts()
            self.session = s
        return self.session


class Fetcher:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        connector: SessionFactory | None = None,
        store: Store | None = None,
    ):
        self.settings = settings or get_settings()
        if connector is None:
            from uarb_agent.uarb.connector import Connector

            connector = Connector(self.settings)
        self.connector = connector
        self.store = store or Store(self.settings.cache_dir)
        self.download_budget_s = DOWNLOAD_BUDGET_S
        self.hearing_budget_s = dict(HEARING_BUDGET_S)
        self._index: SearchIndex | None = None

    def budget_s(self, doc_type: DocType | None) -> float:
        """Seconds into a fetch after which downloads of `doc_type` stop."""
        return self.hearing_budget_s.get(doc_type, self.download_budget_s)

    @property
    def index(self) -> SearchIndex:
        """The full-text index of everything downloaded (data/search.sqlite3), opened on first use."""
        if self._index is None:
            self._index = SearchIndex(index_path(self.settings))
        return self._index

    async def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None
        await self.connector.close()

    async def fetch(
        self,
        matter: str,
        doc_type: DocType | None,
        limit: int = 10,
        offset: int = 0,
        *,
        trace_id: str | None = None,
        retry: bool = True,
    ) -> MatterReport:
        """Raises InvalidMatterNumber before any navigation, MatterNotFound, or SiteUnavailable/LayoutChanged
        after one retry in a fresh session (documents the first attempt saved come from the cache). The retry
        shares the first attempt's download deadline, so the whole call stays within `request_timeout_s`."""
        matter = normalize_matter(matter)
        doc_type = DocType(doc_type) if doc_type else None
        trace_id = trace_id or new_trace_id()
        offset = max(offset, 0)
        deadline = time.monotonic() + self.budget_s(doc_type)
        try:
            report = await self._fetch(matter, doc_type, limit, offset, trace_id, deadline)
        except (SiteUnavailable, LayoutChanged) as e:
            if not retry:
                raise
            log.warning("core.fetch.retry", matter=matter, error=str(e), trace_id=trace_id)
            report = await self._fetch(matter, doc_type, limit, offset, f"{trace_id}-r", deadline)
        await asyncio.to_thread(self._enrich, report)
        return report

    def _enrich(self, report: MatterReport) -> None:
        """Index what was downloaded and attach the Board's decision line. Blocking (pymupdf, SQLite)."""
        if report.downloaded:
            try:
                self.index.index_report(report)
            except Exception as e:
                log.warning("core.index_failed", trace_id=report.trace_id, error=repr(e))
        try:
            report.decision = render_line(decision_for(report, self.store))
        except Exception as e:
            log.warning("core.decision_failed", trace_id=report.trace_id, error=repr(e))

    async def _fetch(
        self, matter: str, doc_type: DocType | None, limit: int, offset: int, trace_id: str, deadline: float
    ) -> MatterReport:
        t0 = time.monotonic()
        tmp = self.settings.data_dir / "tmp" / trace_id
        try:
            async with AsyncExitStack() as stack:
                site = _Site(self.connector, stack, matter, trace_id)
                cached = self.store.get_matter(matter)
                if cached is None:
                    await site.get()
                    assert site.metadata is not None
                    metadata, counts = site.metadata, site.counts
                    self.store.put_matter(matter, metadata, counts)
                else:
                    metadata, counts = cached
                report = MatterReport(
                    metadata=metadata, counts=counts, requested=doc_type, offset=offset, trace_id=trace_id
                )
                if doc_type is not None and counts.get(doc_type, 0) > 0:
                    report.rows = await self._rows(site, matter, doc_type, counts[doc_type])
                    await self._select(site, report, limit, tmp, deadline=deadline)
                timings = dict(site.session.timings) if site.session else {}
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp, ignore_errors=True)
        timings["total"] = round(time.monotonic() - t0, 3)
        report.timings = timings
        return report

    async def _rows(self, site: _Site, matter: str, tab: DocType, count: int) -> list[DocRow]:
        rows = self.store.get_rows(matter, tab)
        if rows is None:
            s = await site.get()
            rows = await s.list_documents(tab, site.counts.get(tab, count))
            self.store.put_rows(matter, tab, rows)
        return newest_first(rows)

    async def _select(self, site: _Site, report: MatterReport, limit: int, tmp: Path, *, deadline: float) -> None:
        """Walk rows from `offset`, filling up to `limit` documents from the cache first, then the site. Each
        download is stored as soon as it lands and cut off at `deadline`, so a slow file never costs the ones
        already fetched. A row cut off is left unreported, so MORE retries it, unless nothing else came back."""
        matter, tab = report.metadata.matter_no, report.requested
        assert tab is not None
        got = 0
        for row in report.rows[report.offset :]:
            if got >= limit:
                break
            if reason := not_public_reason(row):
                report.skipped.append(Skipped(doc_no=row.doc_no, title=row.title, reason=reason))
                continue
            if hit := self.store.get_doc(matter, tab, row.doc_no):
                report.files += [_ok(row, f.path, f.size, f.sha256) for f in hit]
                got += 1
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                log.info("core.download_budget_spent", matter=matter, tab=str(tab), got=got, trace_id=report.trace_id)
                break
            session = await site.get()
            try:
                results = await asyncio.wait_for(session.download([row], tmp), timeout=left)
            except TimeoutError:
                log.info("core.download_cut_off", matter=matter, doc_no=row.doc_no, got=got, trace_id=report.trace_id)
                if not got:  # otherwise MORE would ask for this same file again and time out again
                    error = f"did not finish downloading within {round(self.budget_s(tab))} s"
                    report.files.append(FileResult(doc_no=row.doc_no, title=row.title, error=error))
                break
            saved = [r.path for r in results if r.ok and r.path]
            if saved and await asyncio.to_thread(_any_notice, saved):
                log.warning("core.notice_served", matter=matter, doc_no=row.doc_no, trace_id=report.trace_id)
                report.skipped.append(Skipped(doc_no=row.doc_no, title=row.title, reason=NOTICE_REASON))
                continue
            if saved:
                stored = self.store.put_doc(matter, tab, row.doc_no, saved)
                report.files += [_ok(row, c.path, c.size, c.sha256) for c in stored]
                got += 1
            else:
                error = next((r.error for r in results if r.error), None) or "download failed"
                report.files.append(FileResult(doc_no=row.doc_no, title=row.title, error=error))

    async def find_doc(
        self, matter: str, doc_no: str, prefer: DocType | None = None, *, trace_id: str | None = None
    ) -> MatterReport | None:
        """One document by number (the GET command), searched in `prefer` first. None if no tab lists it."""
        summary = await self.fetch(matter, None, trace_id=trace_id)
        tabs = sorted((t for t, n in summary.counts.items() if n), key=lambda t: t != prefer)
        for tab in tabs:
            listing = await self.fetch(matter, tab, limit=0, trace_id=trace_id)
            for i, row in enumerate(listing.rows):
                if row.doc_no.lower() == doc_no.strip().lower():
                    return await self.fetch(matter, tab, limit=1, offset=i, trace_id=trace_id)
        return None

    def pack(self, report: MatterReport, out_dir: Path, *, budget_bytes: int | None = None) -> PackResult | None:
        """ZIP the report's downloads. With a public_base_url, files too large for the ZIP are published as
        download links and get their `url` set. Whatever the zipper leaves out moves from `files` to `skipped`,
        so the reply never claims a file is delivered when it is not."""
        if report.requested is None or not report.downloaded:
            return None
        budget = self.settings.zip_budget_mb * MB if budget_bytes is None else budget_bytes
        publish = None
        if base_url := self.settings.public_base_url:
            publish = partial(publish_link, links_dir=self.settings.links_dir, base_url=base_url)
        packed = build_zip(
            out_dir,
            report.metadata.matter_no,
            report.requested,
            report.files,
            report.rows,
            budget_bytes=budget,
            summary=report.decision,
            publish=publish,
        )
        kept = {p.sha256 for p in packed.included}
        urls = {p.sha256: p.url for p in packed.linked}
        left_out = {s.doc_no: s for s in packed.skipped}
        files, extra = [], []
        for f in report.files:
            if not f.ok or f.sha256 in kept:
                files.append(f)
            elif f.sha256 in urls:
                files.append(f.model_copy(update={"url": urls[f.sha256]}))
            elif s := left_out.pop(f.doc_no, None):
                extra.append(s)
        report.files = files
        report.skipped += extra
        return packed


def _any_notice(paths: list[Path]) -> bool:
    return any(is_confidentiality_notice(p) for p in paths)


def _ok(row: DocRow, path: Path, size: int, sha256: str) -> FileResult:
    return FileResult(doc_no=row.doc_no, title=row.title, path=path, size=size, sha256=sha256, ok=True)


def as_error(exc: BaseException, trace_id: str | None) -> AgentError:
    """Anything unexpected becomes an honest 'the site is not responding as expected' error with a trace id."""
    if isinstance(exc, AgentError):
        if exc.trace_id is None:
            exc.trace_id = trace_id
        return exc
    if isinstance(exc, TimeoutError):
        return SiteUnavailable("timed out", trace_id=trace_id)
    return SiteUnavailable(f"{type(exc).__name__}: {exc}", trace_id=trace_id)


_default: Fetcher | None = None


async def fetch(matter: str, doc_type: DocType | None, limit: int = 10, offset: int = 0) -> MatterReport:
    """Read metadata and all five counts for `matter`; if `doc_type` is given, list that tab and download up to
    `limit` documents starting at `offset`. Uses one process-wide Fetcher, so call it from a single event loop."""
    global _default
    if _default is None:
        _default = Fetcher()
    return await _default.fetch(matter, doc_type, limit, offset)
