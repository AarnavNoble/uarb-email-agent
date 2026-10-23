"""A stand-in for uarb.Connector that serves a made-up matter and writes small files instead of downloading."""

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path

from uarb_agent.models import (
    DocRow,
    DocType,
    FileResult,
    MatterMetadata,
    MatterNotFound,
    SiteUnavailable,
    normalize_matter,
)

METADATA = MatterMetadata(
    matter_no="M12205",
    title="Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project",
    type="Water",
    category="Capital Expenditure Approvals",
    date_received=dt.date(2025, 4, 7),
    date_final=dt.date(2025, 10, 23),
)
COUNTS = {
    DocType.EXHIBITS: 3,
    DocType.KEY_DOCUMENTS: 2,
    DocType.OTHER_DOCUMENTS: 14,
    DocType.TRANSCRIPTS: 0,
    DocType.RECORDINGS: 0,
}


def make_rows(tab: DocType, n: int, confidential: frozenset[int] = frozenset()) -> list[DocRow]:
    return [
        DocRow(
            doc_no=str(200100 - i),
            title=f"{tab} item {i + 1}",
            date=dt.date(2026, 6, 1) - dt.timedelta(days=i),
            ext=".pdf",
            security="Confidential" if i in confidential else "Public",
            tab=tab,
        )
        for i in range(n)
    ]


class FakeSession:
    def __init__(self, site: FakeSite):
        self.site = site
        self.timings: dict[str, float] = {"open": 0.01}
        self.matter: str | None = None

    async def open_matter(self, matter: str) -> None:
        self.site.opened += 1
        if self.site.down:
            raise SiteUnavailable("down")
        if self.site.delay:
            await asyncio.sleep(self.site.delay)
        self.matter = normalize_matter(matter)
        if self.matter != "M12205":
            raise MatterNotFound(self.matter)

    async def read_metadata(self) -> MatterMetadata:
        return METADATA

    async def read_counts(self) -> dict[DocType, int]:
        return dict(self.site.counts)

    async def list_documents(self, tab: DocType, count: int | None = None) -> list[DocRow]:
        self.site.listed.append(tab)
        return list(self.site.rows[tab])

    async def download(self, rows: list[DocRow], dest: Path) -> list[FileResult]:
        dest.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - test fake, tiny files
        out = []
        for row in rows:
            self.site.downloaded.append(row.doc_no)
            if self.site.download_delay:
                await asyncio.sleep(self.site.download_delay)
            if row.doc_no in self.site.broken:
                out.append(FileResult(doc_no=row.doc_no, title=row.title, error="TimeoutError: no download"))
                continue
            path = dest / f"{row.doc_no}.pdf"
            path.write_bytes(b"%PDF-1.4 " + row.doc_no.encode() * self.site.file_size)
            out.append(FileResult(doc_no=row.doc_no, title=row.title, path=path, size=path.stat().st_size, ok=True))
        return out


class FakeSite:
    """Implements Connector.session()/close(); records what the fetcher asked the 'site' to do."""

    def __init__(self, *, counts=None, rows=None, broken=(), down=False, delay=0.0, file_size=100, download_delay=0.0):
        self.counts = dict(counts or COUNTS)
        self.rows = rows or {t: make_rows(t, n) for t, n in self.counts.items()}
        self.broken = set(broken)
        self.down = down
        self.delay = delay
        self.file_size = file_size
        self.download_delay = download_delay
        self.opened = 0
        self.listed: list[DocType] = []
        self.downloaded: list[str] = []
        self.closed = False

    @asynccontextmanager
    async def session(self, trace_id: str | None = None):
        yield FakeSession(self)

    async def close(self) -> None:
        self.closed = True
