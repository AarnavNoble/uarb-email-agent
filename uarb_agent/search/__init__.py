"""Searchable database of every document the agent has downloaded (SQLite FTS5, one row per PDF page)."""

from uarb_agent.search.cli import format_hits, index_path, open_index, run_index, run_search, search
from uarb_agent.search.index import Hit, IndexStats, SearchIndex, fts_query

__all__ = [
    "Hit",
    "IndexStats",
    "SearchIndex",
    "format_hits",
    "fts_query",
    "index_path",
    "open_index",
    "run_index",
    "run_search",
    "search",
]
