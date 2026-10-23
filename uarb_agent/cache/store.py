"""On-disk cache: immutable downloaded documents plus short-lived matter listings.

Layout under root (normally settings.cache_dir):
  blobs/<sha[:2]>/<sha256><ext>              file bytes, content-addressed
  docs/<matter>/<tab>/<doc_no>.json          which blobs a document resolved to (a modal can hold several)
  listings/<matter>/matter.json              metadata + tab counts, TTL
  listings/<matter>/<tab>.json               rows of one tab, TTL

UARB documents never change once filed, so a document record is written once and trusted after that. Listings do
change (new filings arrive), so they expire. Every write goes through a temp file and os.replace.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import BaseModel, ValidationError

from uarb_agent.models import DocRow, DocType, MatterMetadata

LISTING_TTL_S = 600.0

_UNSAFE = re.compile(r"[^A-Za-z0-9._()-]+")


class CachedFile(BaseModel):
    name: str  # filename the site offered, e.g. '312345.pdf'
    path: Path
    size: int
    sha256: str


class _DocRecord(BaseModel):
    doc_no: str
    files: list[CachedFile]
    stored_at: float


class _MatterEntry(BaseModel):
    stored_at: float
    metadata: MatterMetadata
    counts: dict[DocType, int]


class _RowsEntry(BaseModel):
    stored_at: float
    rows: list[DocRow]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _key(part: str) -> str:
    """Filesystem-safe and collision-resistant: readable slug plus a short hash of the exact value."""
    slug = _UNSAFE.sub("_", part).strip("._")[:60] or "x"
    if slug == part:
        return slug
    return f"{slug}~{hashlib.sha256(part.encode()).hexdigest()[:8]}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, root: Path, *, ttl_s: float = LISTING_TTL_S, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.ttl_s = ttl_s
        self.clock = clock

    # documents

    def _doc_path(self, matter: str, tab: DocType, doc_no: str) -> Path:
        return self.root / "docs" / _key(matter) / _key(tab.value) / f"{_key(doc_no)}.json"

    def _blob_path(self, sha: str, ext: str) -> Path:
        return self.root / "blobs" / sha[:2] / f"{sha}{ext.lower()}"

    def put_blob(self, src: Path, name: str | None = None) -> CachedFile:
        """Copy src into the blob store (no-op if the same bytes are already there). src is left untouched."""
        src = Path(src)
        name = name or src.name
        sha = sha256_file(src)
        dest = self._blob_path(sha, Path(name).suffix)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
            os.close(fd)
            try:
                shutil.copyfile(src, tmp)
                os.replace(tmp, dest)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return CachedFile(name=name, path=dest, size=dest.stat().st_size, sha256=sha)

    def put_doc(
        self, matter: str, tab: DocType, doc_no: str, files: Iterable[Path | tuple[Path, str]]
    ) -> list[CachedFile]:
        """Store every file of one document and record them. Items are paths or (path, offered_name) pairs."""
        cached = []
        for item in files:
            src, name = item if isinstance(item, tuple) else (item, None)
            cached.append(self.put_blob(src, name))
        if not cached:
            raise ValueError(f"no files for {matter} {tab} {doc_no}")
        record = _DocRecord(doc_no=doc_no, files=cached, stored_at=self.clock())
        _atomic_write_bytes(self._doc_path(matter, tab, doc_no), record.model_dump_json(indent=1).encode())
        return cached

    def get_doc(self, matter: str, tab: DocType, doc_no: str) -> list[CachedFile] | None:
        """Cached files for a document, or None if it was never stored or a blob has gone missing or changed size."""
        path = self._doc_path(matter, tab, doc_no)
        try:
            record = _DocRecord.model_validate_json(path.read_bytes())
        except (FileNotFoundError, ValidationError):
            return None
        for f in record.files:
            try:
                if f.path.stat().st_size != f.size:
                    return None
            except FileNotFoundError:
                return None
        return record.files

    # listings (TTL)

    def _listing_path(self, matter: str, name: str) -> Path:
        return self.root / "listings" / _key(matter) / f"{_key(name)}.json"

    def _fresh(self, stored_at: float) -> bool:
        return 0 <= self.clock() - stored_at < self.ttl_s

    def put_matter(self, matter: str, metadata: MatterMetadata, counts: dict[DocType, int]) -> None:
        entry = _MatterEntry(stored_at=self.clock(), metadata=metadata, counts=counts)
        _atomic_write_bytes(self._listing_path(matter, "matter"), entry.model_dump_json().encode())

    def get_matter(self, matter: str) -> tuple[MatterMetadata, dict[DocType, int]] | None:
        try:
            entry = _MatterEntry.model_validate_json(self._listing_path(matter, "matter").read_bytes())
        except (FileNotFoundError, ValidationError):
            return None
        return (entry.metadata, entry.counts) if self._fresh(entry.stored_at) else None

    def put_rows(self, matter: str, tab: DocType, rows: list[DocRow]) -> None:
        entry = _RowsEntry(stored_at=self.clock(), rows=rows)
        _atomic_write_bytes(self._listing_path(matter, tab.value), entry.model_dump_json().encode())

    def get_rows(self, matter: str, tab: DocType, *, stale_ok: bool = False) -> list[DocRow] | None:
        """Rows of one tab while the listing is fresh. With stale_ok, any cached listing: good enough to name a
        document already downloaded (documents never change), not to say what the tab holds now."""
        try:
            entry = _RowsEntry.model_validate_json(self._listing_path(matter, tab.value).read_bytes())
        except (FileNotFoundError, ValidationError):
            return None
        return entry.rows if stale_ok or self._fresh(entry.stored_at) else None

    def invalidate(self, matter: str) -> None:
        """Drop the listings for a matter (documents stay; they are immutable)."""
        shutil.rmtree(self.root / "listings" / _key(matter), ignore_errors=True)


__all__ = ["LISTING_TTL_S", "CachedFile", "Store", "sha256_file"]
