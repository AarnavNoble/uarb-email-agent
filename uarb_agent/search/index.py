"""Full-text index over every document the agent has fetched: SQLite FTS5, one row per PDF page.

Hits cite a document and a page, so a reply can say "102674 p.1" and offer "GET 102674". Only documents already
in the cache are searchable; nothing here talks to the UARB site. Pages without a text layer (scans) are kept and
flagged has_text=0 rather than OCR'd, so coverage is never overstated.

A document is identified by (matter, tab, doc_no, sha256). Indexing the same one again is a no-op, except that a
known title or date fills in one that was missing (a backfill can run before the listing was cached).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import structlog
from pydantic import ValidationError

from uarb_agent.cache.store import CachedFile, Store, _DocRecord, _key, sha256_file
from uarb_agent.models import DocRow, DocType, InvalidMatterNumber, MatterReport, normalize_matter

log = structlog.get_logger(__name__)

# Fewer word characters than this on a page counts as no text layer (a scan, or just a stamp or page number).
_MIN_WORD_CHARS = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    matter TEXT NOT NULL,
    tab TEXT NOT NULL,
    doc_no TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    file_name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    date TEXT,
    pages INTEGER NOT NULL DEFAULT 0,
    text_pages INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,  -- 'ok' | 'not_pdf' | 'error'
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (matter, tab, doc_no, sha256)
);
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    text, title,
    matter UNINDEXED, tab UNINDEXED, doc_no UNINDEXED, sha256 UNINDEXED,
    date UNINDEXED, page UNINDEXED, has_text UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True)
class Hit:
    matter: str
    tab: str
    doc_no: str
    title: str
    date: str | None
    page: int  # 1-based
    snippet: str
    all_terms: bool  # False when no page held every term and this came from the any-term fallback

    def cite(self) -> str:
        return f"{self.doc_no} p.{self.page}"


@dataclass(frozen=True)
class IndexStats:
    documents: int
    pages: int
    image_only_pages: int
    matters: int
    skipped: int  # non-PDF or unreadable files


# query sanitising


MAX_TERMS = 32  # past this a query is a pasted page, not a search; the rest is ignored
_QUOTED = re.compile(r'"([^"]*)"')
_WORD = re.compile(r"\w+")


def fts_query(text: str, *, any_term: bool = False) -> str | None:
    """User text -> a safe FTS5 MATCH string, or None if it holds no searchable words.

    Every term is emitted as a quoted phrase so FTS syntax (AND, NEAR, '-', '*', ':', parentheses) in user text is
    inert. "semi-annual" becomes the phrase "semi annual"; "quoted text" stays one phrase; terms are ANDed
    (or ORed with any_term).
    """
    phrases: list[str] = []

    def add(chunk: str) -> None:
        words = _WORD.findall(chunk)
        if words:
            phrases.append('"' + " ".join(words) + '"')

    pos = 0
    for m in _QUOTED.finditer(text):
        for token in text[pos : m.start()].split():
            add(token)
        add(m.group(1))
        pos = m.end()
    for token in text[pos:].replace('"', " ").split():
        add(token)
    if not phrases:
        return None
    return (" OR " if any_term else " ").join(list(dict.fromkeys(phrases))[:MAX_TERMS])


# extraction


def extract_pages(path: Path) -> list[str]:
    """Plain text of each page of a PDF ('' for a page with no text layer). Raises on an unreadable file."""
    with pymupdf.open(path) as doc:
        return [page.get_text("text") for page in doc]


def has_text(text: str) -> bool:
    return sum(len(w) for w in _WORD.findall(text)) >= _MIN_WORD_CHARS


def _is_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(1024).lstrip().startswith(b"%PDF")
    except OSError:
        return False


# the index


class SearchIndex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The worker may call this from asyncio.to_thread while a CLI backfill runs: one lock per connection,
        # WAL plus a busy timeout across processes.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SearchIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # writing

    def index_document(
        self,
        *,
        matter: str,
        tab: DocType | str,
        doc_no: str,
        path: Path,
        title: str = "",
        date: dt.date | str | None = None,
        sha256: str | None = None,
        file_name: str | None = None,
    ) -> int:
        """Index one file of a document. Returns the number of pages added (0 if it was already indexed)."""
        path = Path(path)
        tab = str(tab)
        sha = sha256 or sha256_file(path)
        date_s = date.isoformat() if isinstance(date, dt.date) else date
        key = (matter, tab, doc_no, sha)
        with self._lock:
            row = self._db.execute(
                "SELECT title, date FROM docs WHERE matter=? AND tab=? AND doc_no=? AND sha256=?", key
            ).fetchone()
            if row is not None:
                self._fill_in(key, row, title, date_s)
                return 0
        status, texts = "ok", []
        if not _is_pdf(path):
            status = "not_pdf"
        else:
            try:
                texts = extract_pages(path)
            except Exception as e:  # pymupdf raises its own types for damaged files
                log.warning("search.extract_failed", matter=matter, doc_no=doc_no, path=str(path), error=str(e))
                status = "error"
        flags = [has_text(t) for t in texts]
        with self._lock, self._db:
            inserted = self._db.execute(
                "INSERT OR IGNORE INTO docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, file_name or path.name, title, date_s, len(texts), sum(flags), status, _now()),
            ).rowcount
            if not inserted:  # another thread indexed it while this one was extracting
                return 0
            self._db.executemany(
                "INSERT INTO pages (text, title, matter, tab, doc_no, sha256, date, page, has_text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (t if ok else "", title, *key, date_s, i, int(ok))
                    for i, (t, ok) in enumerate(zip(texts, flags, strict=True), 1)
                ],
            )
        log.info("search.indexed", matter=matter, tab=tab, doc_no=doc_no, pages=len(texts), status=status)
        return len(texts)

    def _fill_in(self, key: tuple[str, ...], row: tuple[str, str | None], title: str, date: str | None) -> None:
        old_title, old_date = row
        new_title = title or old_title
        new_date = date or old_date
        if (new_title, new_date) == (old_title, old_date):
            return
        where = "matter=? AND tab=? AND doc_no=? AND sha256=?"
        with self._db:
            self._db.execute(f"UPDATE docs SET title=?, date=? WHERE {where}", (new_title, new_date, *key))
            self._db.execute(f"UPDATE pages SET title=?, date=? WHERE {where}", (new_title, new_date, *key))

    def index_report(self, report: MatterReport) -> int:
        """Index every downloaded file of a fetch. Returns pages added."""
        if report.requested is None:
            return 0
        rows = {r.doc_no: r for r in report.rows}
        added = 0
        for f in report.downloaded:
            if f.path is None:
                continue
            row = rows.get(f.doc_no)
            added += self.index_document(
                matter=report.metadata.matter_no,
                tab=report.requested,
                doc_no=f.doc_no,
                path=f.path,
                title=f.title,
                date=row.date if row else None,
                sha256=f.sha256,
            )
        return added

    def backfill(self, store: Store) -> int:
        """Index everything already in the cache. Returns pages added; safe to rerun."""
        added = 0
        for matter, tab, doc_no, path, sha, name, row in _cached_docs(store):
            try:
                added += self.index_document(
                    matter=matter,
                    tab=tab,
                    doc_no=doc_no,
                    path=path,
                    title=row.title if row else "",
                    date=row.date if row else None,
                    sha256=sha,
                    file_name=name,
                )
            except OSError as e:
                log.warning("search.backfill_skip", matter=matter, doc_no=doc_no, error=str(e))
        return added

    # reading

    def search(self, query: str, matter: str | None = None, limit: int = 5, *, per_doc: int = 1) -> list[Hit]:
        """Best-matching pages, at most `per_doc` per document number. Pages holding the words as one phrase come
        first, then pages holding every term; if no page has them all, falls back to any term (those hits have
        all_terms=False). Never raises on odd user text."""
        if matter is not None:
            try:
                matter = normalize_matter(matter)
            except InvalidMatterNumber:
                return []
        every = fts_query(query)
        if every is None:
            return []
        seen: dict[tuple[str, str], int] = {}  # hits per (matter, doc_no): one doc_no can be under two tabs
        hits: list[Hit] = []
        words = _WORD.findall(query)[:MAX_TERMS]
        if len(words) > 1:  # BM25 alone ignores word order: 'semi-annual reports' should beat a page of 'reports'
            hits = self._run('"' + " ".join(words) + '"', matter, limit, per_doc, seen, all_terms=True)
        hits += self._run(every, matter, limit - len(hits), per_doc, seen, all_terms=True)
        some = fts_query(query, any_term=True)
        if hits or some == every:  # a single term has no looser form
            return hits
        return self._run(some, matter, limit, per_doc, seen, all_terms=False)

    def _run(
        self, q: str, matter: str | None, limit: int, per_doc: int, seen: dict[tuple[str, str], int], *, all_terms: bool
    ) -> list[Hit]:
        sql = (
            "SELECT matter, tab, doc_no, title, date, page,"
            " snippet(pages, 0, '**', '**', ' ... ', 16) FROM pages WHERE pages MATCH ?"
            + (" AND matter = ?" if matter else "")
            + " ORDER BY bm25(pages, 1.0, 2.0)"
        )
        params = (q, matter) if matter else (q,)
        hits: list[Hit] = []
        if limit <= 0:
            return hits
        with self._lock:
            try:
                cursor = self._db.execute(sql, params)
            except sqlite3.OperationalError as e:  # should not happen after fts_query; never surface it
                log.warning("search.query_failed", query=q, error=str(e))
                return []
            for m, tab, doc_no, title, date, page, snip in cursor:
                doc = (m, doc_no)
                if seen.get(doc, 0) >= per_doc:
                    continue
                seen[doc] = seen.get(doc, 0) + 1
                hits.append(Hit(m, tab, doc_no, title, date, page, _tidy(snip) or "(title match)", all_terms))
                if len(hits) >= limit:
                    break
        return hits

    def stats(self, matter: str | None = None) -> IndexStats:
        """What is searchable, overall or for one matter. A document listed under two tabs (a Board Order in Key
        and Other Documents) counts once, and so do its pages, so a reply never overstates what was searched."""
        if matter is not None:
            try:
                matter = normalize_matter(matter)
            except InvalidMatterNumber:
                return IndexStats(0, 0, 0, 0, 0)
        where, params = (" WHERE matter = ?", (matter,)) if matter else ("", ())
        with self._lock:
            docs, readable, matters = self._db.execute(
                "SELECT COUNT(DISTINCT matter || '/' || doc_no),"
                " COUNT(DISTINCT CASE WHEN status = 'ok' THEN matter || '/' || doc_no END),"
                " COUNT(DISTINCT matter) FROM docs" + where,
                params,
            ).fetchone()
            pages, image_only = self._db.execute(
                "SELECT SUM(pages), SUM(pages - text_pages)"
                " FROM (SELECT DISTINCT matter, sha256, pages, text_pages FROM docs" + where + ")",
                params,
            ).fetchone()
        return IndexStats(docs, pages or 0, image_only or 0, matters, docs - readable)


def _tidy(snippet: str) -> str:
    return " ".join(snippet.split())


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


# walking the cache


_TABS_BY_KEY = {_key(t.value): t for t in DocType}


def _cached_docs(store: Store) -> Iterator[tuple[str, DocType, str, Path, str, str, DocRow | None]]:
    """(matter, tab, doc_no, blob path, sha256, offered name, listing row or None) for every cached file.

    Titles and dates come from the cached listings, read regardless of their TTL (a stale listing still names
    an immutable document correctly)."""
    docs_root = store.root / "docs"
    for record_path in sorted(docs_root.glob("*/*/*.json")):
        tab = _TABS_BY_KEY.get(record_path.parent.name)
        if tab is None:
            continue
        try:
            record = _DocRecord.model_validate_json(record_path.read_bytes())
            matter = normalize_matter(record_path.parent.parent.name)
        except (OSError, ValidationError, InvalidMatterNumber):
            continue
        row = _listing_rows(store, matter, tab).get(record.doc_no)
        for f in record.files:
            path = _blob(store, f)
            if path is not None:
                yield matter, tab, record.doc_no, path, f.sha256, f.name, row


def _blob(store: Store, f: CachedFile) -> Path | None:
    """The recorded path may be relative to wherever the agent ran; fall back to the content address."""
    for candidate in (f.path, store.root / "blobs" / f.sha256[:2] / f"{f.sha256}{Path(f.name).suffix.lower()}"):
        if candidate.is_file():
            return candidate
    return None


def _listing_rows(store: Store, matter: str, tab: DocType) -> dict[str, DocRow]:
    path = store.root / "listings" / _key(matter) / f"{_key(tab.value)}.json"
    try:
        raw = json.loads(path.read_bytes())
        rows = [DocRow.model_validate(r) for r in raw.get("rows", [])]
    except (OSError, ValueError, ValidationError):
        return {}
    return {r.doc_no: r for r in rows}


__all__ = ["Hit", "IndexStats", "SearchIndex", "extract_pages", "fts_query", "has_text"]
