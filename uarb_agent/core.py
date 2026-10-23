"""The one entry point every channel (email worker, CLI) calls: matter in, MatterReport out.

`Fetcher.fetch` reads metadata and the five counts, lists the requested tab, and downloads up to `limit`
documents starting at `offset`, going to the site only for what the cache cannot answer. Rows are taken newest
first by filing date: the site lists Other Documents that way, but Key Documents and Exhibits oldest first.
Confidential rows are passed over and reported as skipped, because the site serves a one-page confidentiality
notice in their place. A row whose download fails (the connector has already retried it once) is reported and
the next row is taken instead.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Protocol

import structlog

from uarb_agent.cache.store import Store
from uarb_agent.config import Settings, get_settings
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
from uarb_agent.pack.zipper import MB, PackResult, build_zip
from uarb_agent.uarb.connector import new_trace_id

log = structlog.get_logger(__name__)

CONFIDENTIAL_REASON = "confidential; the public site only offers a notice in its place"
# No new download starts after this many seconds into a fetch. Exhibits can be 40+ MB each (about 40 s apiece on
# M12205), so ten of them would blow the worker's per-request timeout and lose the whole batch. Stopping early
# returns what was fetched; MORE resumes at the first row not attempted.
DOWNLOAD_BUDGET_S = 80.0


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


def is_public(row: DocRow) -> bool:
    return (row.security or "").strip().lower() != "confidential"


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

    async def close(self) -> None:
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
        after one retry in a fresh session (documents the first attempt saved come from the cache)."""
        matter = normalize_matter(matter)
        doc_type = DocType(doc_type) if doc_type else None
        trace_id = trace_id or new_trace_id()
        offset = max(offset, 0)
        try:
            return await self._fetch(matter, doc_type, limit, offset, trace_id)
        except (SiteUnavailable, LayoutChanged) as e:
            if not retry:
                raise
            log.warning("core.fetch.retry", matter=matter, error=str(e), trace_id=trace_id)
            return await self._fetch(matter, doc_type, limit, offset, f"{trace_id}-r")

    async def _fetch(
        self, matter: str, doc_type: DocType | None, limit: int, offset: int, trace_id: str
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
                    await self._select(site, report, limit, tmp, deadline=t0 + self.download_budget_s)
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
        download is stored as soon as it lands, so a timeout later on never throws away finished work."""
        matter, tab = report.metadata.matter_no, report.requested
        assert tab is not None
        got = 0
        for row in report.rows[report.offset :]:
            if got >= limit:
                break
            if not is_public(row):
                report.skipped.append(Skipped(doc_no=row.doc_no, title=row.title, reason=CONFIDENTIAL_REASON))
                continue
            if hit := self.store.get_doc(matter, tab, row.doc_no):
                report.files += [_ok(row, f.path, f.size, f.sha256) for f in hit]
                got += 1
                continue
            if time.monotonic() >= deadline:
                log.info("core.download_budget_spent", matter=matter, tab=str(tab), got=got, trace_id=report.trace_id)
                break
            results = await (await site.get()).download([row], tmp)
            saved = [r.path for r in results if r.ok and r.path]
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
        """ZIP the report's downloads. Whatever the zipper leaves out moves from `files` to `skipped`, so the
        reply never claims a file is attached when it is not."""
        if report.requested is None or not report.downloaded:
            return None
        budget = self.settings.zip_budget_mb * MB if budget_bytes is None else budget_bytes
        packed = build_zip(
            out_dir, report.metadata.matter_no, report.requested, report.files, report.rows, budget_bytes=budget
        )
        kept = {p.sha256 for p in packed.included}
        left_out = {s.doc_no: s for s in packed.skipped}
        files, extra = [], []
        for f in report.files:
            if not f.ok or f.sha256 in kept:
                files.append(f)
            elif s := left_out.pop(f.doc_no, None):
                extra.append(s)
        report.files = files
        report.skipped += extra
        return packed


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
