"""Read-only MCP server (stdio): the same Fetcher the email agent uses, exposed as tools for an MCP client.

uarb-agent mcp            (or: python -m uarb_agent.mcp_server)

Tools: get_matter, list_documents, download_documents, search. Each is a thin wrapper over core.Fetcher, so the
cache, the two-session limit on the UARB site and the confidential-row rules all apply exactly as they do for
email. Nothing here writes to the site; downloads land in the local cache and a ZIP under data/out/.
stdout carries the protocol, so all logging must go to stderr.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from uarb_agent.config import Settings, get_settings
from uarb_agent.core import Fetcher, is_public
from uarb_agent.intent.aliases import match_type
from uarb_agent.models import DOC_TYPES, AgentError, DocRow, DocType, MatterMetadata, MatterReport, Skipped

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
MAX_LIST = 200


class MatterInfo(BaseModel):
    metadata: MatterMetadata
    counts: dict[str, int]  # every tab, site order, zeros included
    total: int  # all files in the matter across the five tabs
    decision: str | None = None  # cited line from a cached Board Order/Decision; None when none is cached


class Document(BaseModel):
    doc_no: str
    title: str
    date: str | None = None
    ext: str | None = None
    public: bool


class DocumentList(BaseModel):
    matter: str
    doc_type: str
    total: int  # rows on the tab
    offset: int
    documents: list[Document]  # newest first, the order downloads take


class DownloadedFile(BaseModel):
    doc_no: str
    title: str
    path: str | None = None  # absolute path of the cached file on this machine
    size: int = 0
    url: str | None = None  # expiring download link, when PUBLIC_BASE_URL is set and the file was too big to ZIP
    error: str | None = None


class Download(BaseModel):
    matter: str
    doc_type: str
    available: int  # rows on the tab
    offset: int
    zip_path: str | None = None
    files: list[DownloadedFile]
    skipped: list[Skipped]
    next_offset: int | None = None  # pass back as offset to continue; None when the tab is exhausted


def parse_doc_type(value: str) -> DocType:
    """'Other Documents', 'other docs', 'exhibits' ... -> DocType; ToolError listing the valid names otherwise."""
    try:
        return DocType(value)
    except ValueError:
        pass
    if t := match_type(value):
        return t
    raise ToolError(f"unknown document type {value!r}; use one of: {', '.join(DOC_TYPES)}")


def _document(row: DocRow) -> Document:
    return Document(
        doc_no=row.doc_no,
        title=row.title,
        date=row.date.isoformat() if row.date else None,
        ext=row.ext,
        public=is_public(row),
    )


def _plain(hit: Any) -> Any:
    if isinstance(hit, BaseModel):
        return hit.model_dump(mode="json")
    if hasattr(hit, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(hit)
    return hit


class Tools:
    """The tool bodies, separate from MCP registration so tests can drive them with a fake Fetcher."""

    def __init__(self, settings: Settings | None = None, fetcher_factory: Callable[[], Fetcher] | None = None):
        self.settings = settings or get_settings()
        self._factory = fetcher_factory or (lambda: Fetcher(self.settings))
        self._fetcher: Fetcher | None = None

    @property
    def fetcher(self) -> Fetcher:
        # Created on first use, so starting the server (and listing tools) never launches a browser.
        if self._fetcher is None:
            self._fetcher = self._factory()
        return self._fetcher

    async def close(self) -> None:
        if self._fetcher is not None:
            await self._fetcher.close()

    async def _fetch(self, matter: str, doc_type: DocType | None, limit: int = 0, offset: int = 0) -> MatterReport:
        try:
            return await self.fetcher.fetch(matter, doc_type, limit, offset)
        except AgentError as e:
            trace = f" (trace {e.trace_id})" if e.trace_id else ""
            raise ToolError(f"{type(e).__name__}: {e}{trace}") from e

    async def get_matter(self, matter: str) -> MatterInfo:
        report = await self._fetch(matter, None)
        counts = {str(t): report.counts.get(t, 0) for t in DOC_TYPES}
        return MatterInfo(metadata=report.metadata, counts=counts, total=sum(counts.values()), decision=report.decision)

    async def list_documents(self, matter: str, doc_type: str, limit: int = 50, offset: int = 0) -> DocumentList:
        tab = parse_doc_type(doc_type)
        report = await self._fetch(matter, tab)
        offset, limit = max(offset, 0), max(0, min(limit, MAX_LIST))
        return DocumentList(
            matter=report.metadata.matter_no,
            doc_type=str(tab),
            total=len(report.rows),
            offset=offset,
            documents=[_document(r) for r in report.rows[offset : offset + limit]],
        )

    async def download_documents(self, matter: str, doc_type: str, limit: int = 10, offset: int = 0) -> Download:
        tab = parse_doc_type(doc_type)
        limit = max(0, min(limit, self.settings.max_docs))
        report = await self._fetch(matter, tab, limit, max(offset, 0))
        # Every downloaded file stays on disk in the cache, even ones the email-sized ZIP has to leave out.
        attempted = list(report.files)
        packed = await asyncio.to_thread(self.fetcher.pack, report, self.settings.data_dir / "out" / report.trace_id)
        urls = {f.doc_no: f.url for f in report.files if f.url}
        files = [
            DownloadedFile(
                doc_no=f.doc_no,
                title=f.title,
                path=str(f.path.resolve()) if f.path else None,
                size=f.size,
                url=urls.get(f.doc_no),
                error=f.error,
            )
            for f in attempted
        ]
        done = {f.doc_no for f in attempted} | {s.doc_no for s in report.skipped}
        last = max((i for i, r in enumerate(report.rows) if r.doc_no in done), default=report.offset - 1)
        return Download(
            matter=report.metadata.matter_no,
            doc_type=str(tab),
            available=len(report.rows),
            offset=report.offset,
            zip_path=str(packed.path.resolve()) if packed and packed.path else None,
            files=files,
            skipped=report.skipped,
            next_offset=last + 1 if last + 1 < len(report.rows) else None,
        )

    async def search(self, query: str, matter: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        backend = self._search_backend()
        limit = max(1, min(limit, 50))
        try:
            if inspect.iscoroutinefunction(backend):
                hits = await backend(query, matter=matter, limit=limit)
            else:
                hits = await asyncio.to_thread(backend, query, matter=matter, limit=limit)
        except AgentError as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
        return [_plain(h) for h in hits]

    def _search_backend(self) -> Callable[..., Any]:
        """uarb_agent.search.search if the package has one, else a query on its open_index(). Imported per call,
        so the server runs (and the other tools work) in a build without the search package."""
        try:
            # import_module reads sys.modules, so a stand-in installed there (tests, a trimmed build) is honoured;
            # `import a.b as m` would return the attribute already bound on the parent package instead.
            mod = importlib.import_module("uarb_agent.search")
        except ImportError as e:
            raise ToolError(f"full-text search is not available in this build ({e})") from e
        if fn := getattr(mod, "search", None):
            return fn
        open_index = getattr(mod, "open_index", None)
        if open_index is None:
            raise ToolError("full-text search is not available in this build")

        def via_index(query: str, matter: str | None = None, limit: int = 5) -> list[Any]:
            with open_index(self.settings) as index:
                return index.search(query, matter=matter, limit=limit)

        return via_index


def build_server(tools: Tools | None = None) -> MCPServer:
    tools = tools or Tools()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await tools.close()

    server = MCPServer(
        "uarb",
        instructions=(
            "Nova Scotia Utility and Review Board (UARB) matters. Matter numbers look like M12205. "
            f"Document types: {', '.join(DOC_TYPES)}. Start with get_matter; list_documents before downloading. "
            "Answers come from the public UARB site and this agent's local cache; cite doc_no when quoting."
        ),
        lifespan=lifespan,
    )

    @server.tool(annotations=READ_ONLY)
    async def get_matter(matter: str) -> MatterInfo:
        """Metadata (title, type, category, status, outcome, filing dates) and per-tab document counts for a
        UARB matter, plus the total across all tabs and, when its Board Order or Decision is already downloaded,
        a one-line cited summary of what the Board decided."""
        return await tools.get_matter(matter)

    @server.tool(annotations=READ_ONLY)
    async def list_documents(matter: str, doc_type: str, limit: int = 50, offset: int = 0) -> DocumentList:
        """List a matter's documents on one tab (Exhibits, Key Documents, Other Documents, Transcripts or
        Recordings), newest first. Confidential rows are listed with public=false and cannot be downloaded."""
        return await tools.list_documents(matter, doc_type, limit, offset)

    @server.tool(annotations=READ_ONLY)
    async def download_documents(matter: str, doc_type: str, limit: int = 10, offset: int = 0) -> Download:
        """Download up to `limit` (max 10) public documents from one tab, newest first from `offset`, into the
        local cache, and ZIP them. Returns local file paths, the ZIP path and, for files too large to ZIP when
        links are configured, download URLs. Use next_offset to continue. Can take a minute or two."""
        return await tools.download_documents(matter, doc_type, limit, offset)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
    async def search(query: str, matter: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Full-text search over documents this agent has already downloaded (not the whole UARB site),
        optionally within one matter. Each hit cites its matter, doc_no and page."""
        return await tools.search(query, matter, limit)

    return server


def run() -> int:
    """Serve over stdio until the client disconnects."""
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
    raise SystemExit(run())
