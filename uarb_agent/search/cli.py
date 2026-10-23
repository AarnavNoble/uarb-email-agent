"""Plain-text entry points for `uarb-agent index` and `uarb-agent search "<q>"`, plus the hit formatter a SEARCH
reply can reuse. Each returns the text to print; nothing here reaches the UARB site."""

from __future__ import annotations

from pathlib import Path

from uarb_agent.cache.store import Store
from uarb_agent.config import Settings, get_settings
from uarb_agent.search.index import Hit, IndexStats, SearchIndex


def index_path(settings: Settings) -> Path:
    return settings.data_dir / "search.sqlite3"


def open_index(settings: Settings | None = None) -> SearchIndex:
    return SearchIndex(index_path(settings or get_settings()))


def describe_stats(s: IndexStats) -> str:
    line = f"{s.documents} files from {s.matters} matters, {s.pages} pages searchable"
    if s.image_only_pages:
        line += f" ({s.image_only_pages} scanned pages have no text layer and are not searchable)"
    if s.skipped:
        line += f"; {s.skipped} non-PDF or unreadable files skipped"
    return line + "."


def format_hits(hits: list[Hit], query: str) -> str:
    """Cited snippets, each followed by the GET command that fetches the document."""
    if not hits:
        return f'No pages in the downloaded documents match "{query}".'
    lines = []
    if not hits[0].all_terms:
        lines += [f'No single page has every word of "{query}"; closest matches:', ""]
    for n, h in enumerate(hits, 1):
        dated = f", {h.date}" if h.date else ""
        title = h.title or "(untitled)"
        lines.append(f"{n}. {h.matter} {h.tab} {h.doc_no} - {title}{dated} [{h.cite()}]")
        lines.append(f"   {h.snippet}")
        lines.append(f"   To receive it, reply: GET {h.doc_no} ({h.matter})")
    return "\n".join(lines)


def run_index(settings: Settings | None = None) -> str:
    """Backfill the index from the cache (idempotent) and summarise what is searchable."""
    settings = settings or get_settings()
    with open_index(settings) as idx:
        added = idx.backfill(Store(settings.cache_dir))
        return f"Indexed {added} new pages. " + describe_stats(idx.stats())


def search(query: str, matter: str | None = None, limit: int = 5, settings: Settings | None = None) -> list[Hit]:
    """One-shot search of the default index (data/search.sqlite3). Blocking; call via asyncio.to_thread."""
    with open_index(settings) as idx:
        return idx.search(query, matter=matter, limit=limit)


def run_search(query: str, matter: str | None = None, limit: int = 5, settings: Settings | None = None) -> str:
    return format_hits(search(query, matter, limit, settings), query)


__all__ = ["describe_stats", "format_hits", "index_path", "open_index", "run_index", "run_search", "search"]
