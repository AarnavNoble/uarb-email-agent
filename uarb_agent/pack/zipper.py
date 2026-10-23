"""Pack downloaded documents into one email-safe ZIP with a manifest.

Email-safe means: the raw bytes fit the attachment budget, nothing inside is a type Gmail refuses to deliver,
nothing is encrypted, and every entry name is plain ASCII that unzips cleanly on Windows and macOS.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from uarb_agent.cache.store import sha256_file
from uarb_agent.models import DocRow, DocType, FileResult, Skipped

MB = 1024 * 1024
DEFAULT_BUDGET_BYTES = 18 * MB
# Room for manifest.csv, README.txt and ZIP headers, so the attachment itself stays under the budget.
_OVERHEAD_RESERVE = 64 * 1024

# Gmail rejects these outright, including inside archives.
BLOCKED_EXTS = frozenset(
    [
        ".ade",
        ".adp",
        ".apk",
        ".appx",
        ".appxbundle",
        ".bat",
        ".cab",
        ".chm",
        ".cmd",
        ".com",
        ".cpl",
        ".diagcab",
        ".diagcfg",
        ".diagpkg",
        ".dll",
        ".dmg",
        ".ex",
        ".ex_",
        ".exe",
        ".hta",
        ".img",
        ".ins",
        ".iso",
        ".isp",
        ".jar",
        ".jnlp",
        ".js",
        ".jse",
        ".lib",
        ".lnk",
        ".mde",
        ".mjs",
        ".msc",
        ".msi",
        ".msix",
        ".msixbundle",
        ".msp",
        ".mst",
        ".nsh",
        ".pif",
        ".ps1",
        ".scr",
        ".sct",
        ".shb",
        ".sys",
        ".vb",
        ".vbe",
        ".vbs",
        ".vhd",
        ".vxd",
        ".wsc",
        ".wsf",
        ".wsh",
        ".xll",
    ]
)
# Archives whose contents we cannot inspect; Gmail may block them, so they are left out.
OPAQUE_ARCHIVE_EXTS = frozenset([".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".arj", ".lzh", ".cab"])

MANIFEST_FIELDS = ["file", "doc_no", "title", "date", "ext", "security", "size", "sha256", "source"]
# Appended to the manifest only when some files went out as download links; the value is the URL.
LINK_FIELD = "link"
_TITLE_MAX = 80
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 ._,&'()\[\]+=#@!-]+")


class PackedFile(BaseModel):
    arcname: str
    doc_no: str
    title: str
    size: int
    sha256: str
    url: str | None = None  # set when the file was published as a download link instead of zipped


class PackResult(BaseModel):
    path: Path | None = None  # None when nothing could be included
    included: list[PackedFile] = Field(default_factory=list)
    linked: list[PackedFile] = Field(default_factory=list)  # too large for the ZIP, published as links
    skipped: list[Skipped] = Field(default_factory=list)

    @property
    def raw_bytes(self) -> int:
        return sum(f.size for f in self.included)


def zip_name(matter: str, tab: DocType) -> str:
    return f"{matter}_{tab.value.replace(' ', '_')}.zip"


def ascii_clean(text: str) -> str:
    """Fold to ASCII, drop characters that are unsafe in filenames, collapse whitespace."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(_UNSAFE_CHARS.sub(" ", folded).split())


def entry_name(doc_no: str, title: str, ext: str) -> str:
    """'<doc_no> - <title><ext>' in safe ASCII, title shortened on a word boundary."""
    doc = ascii_clean(doc_no).replace(" ", "") or "doc"
    t = ascii_clean(title)
    if len(t) > _TITLE_MAX:
        t = t[:_TITLE_MAX].rsplit(" ", 1)[0] if " " in t[:_TITLE_MAX] else t[:_TITLE_MAX]
    t = t.strip(" .")
    stem = f"{doc} - {t}" if t else doc
    return stem.strip(" .") + _clean_ext(ext)


def _clean_ext(ext: str) -> str:
    e = re.sub(r"[^a-z0-9]", "", ext.lower())
    return f".{e}" if e else ""


def _dedupe(name: str, taken: set[str]) -> str:
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate = f"{stem} ({n}){ext}"
        n += 1
    taken.add(candidate.lower())
    return candidate


def _blocked_reason(path: Path, ext: str) -> str | None:
    if ext in BLOCKED_EXTS:
        return f"{ext} files are blocked by Gmail"
    if ext in OPAQUE_ARCHIVE_EXTS:
        return f"{ext} archives can be blocked by Gmail"
    if ext == ".zip":
        try:
            with zipfile.ZipFile(path) as inner:
                for info in inner.infolist():
                    if info.flag_bits & 0x1:
                        return "contains an encrypted ZIP, which Gmail blocks"
                    if os.path.splitext(info.filename)[1].lower() in BLOCKED_EXTS | OPAQUE_ARCHIVE_EXTS | {".zip"}:
                        return f"contains {info.filename!r}, which Gmail may block"
        except zipfile.BadZipFile:
            return "unreadable ZIP file"
    return None


def _fmt_mb(n: int) -> str:
    return f"{n / MB:.1f} MB"


def build_zip(
    out_dir: Path,
    matter: str,
    tab: DocType,
    files: Sequence[FileResult],
    rows: Sequence[DocRow] = (),
    *,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    retrieved_at: dt.datetime | None = None,
    summary: str | None = None,
    publish: Callable[[Path, str], str] | None = None,
) -> PackResult:
    """Write out_dir/<matter>_<Tab>.zip from `files` in the given order, skipping what cannot be sent.

    A file that does not fit the remaining budget is skipped and later, smaller files still get a chance.
    With `publish` (path, filename -> URL), such files go out as download links instead and are listed in the
    manifest. `rows` supplies date and security for the manifest. Failed downloads are reported as skipped.
    """
    retrieved_at = retrieved_at or dt.datetime.now(dt.UTC)
    by_doc = {r.doc_no: r for r in rows}
    limit = max(budget_bytes - _OVERHEAD_RESERVE, 0)
    result = PackResult()
    taken = {"manifest.csv", "readme.txt"}
    chosen: list[tuple[Path, PackedFile, DocRow | None, str]] = []
    linked: list[tuple[Path, PackedFile, DocRow | None, str]] = []
    used = 0

    def skip(f: FileResult, reason: str) -> None:
        result.skipped.append(Skipped(doc_no=f.doc_no, title=f.title, reason=reason))

    for f in files:
        if not f.ok or f.path is None:
            skip(f, f"download failed: {f.error}" if f.error else "download failed")
            continue
        path = Path(f.path)
        if not path.is_file():
            skip(f, "downloaded file is missing")
            continue
        size = path.stat().st_size
        row = by_doc.get(f.doc_no)
        ext = _clean_ext(path.suffix) or _clean_ext((row.ext if row else None) or "")
        if reason := _blocked_reason(path, ext):
            skip(f, reason)
            continue
        if size > limit:
            too_big = f"too large to email ({_fmt_mb(size)}; limit {_fmt_mb(budget_bytes)})"
        elif used + size > limit:
            too_big = f"over the {_fmt_mb(budget_bytes)} attachment budget ({_fmt_mb(size)})"
        else:
            too_big = None
        if too_big and publish is None:
            skip(f, too_big)
            continue
        sha = sha256_file(path)
        if f.sha256 and f.sha256 != sha:
            skip(f, "file changed after download (checksum mismatch)")
            continue
        if too_big and publish:
            name = entry_name(f.doc_no, f.title, ext)
            try:
                url = publish(path, name)
            except OSError:
                skip(f, too_big)
                continue
            packed = PackedFile(arcname=name, doc_no=f.doc_no, title=f.title, size=size, sha256=sha, url=url)
            linked.append((path, packed, row, ext))
            continue
        used += size
        arcname = _dedupe(entry_name(f.doc_no, f.title, ext), taken)
        packed = PackedFile(arcname=arcname, doc_no=f.doc_no, title=f.title, size=size, sha256=sha)
        chosen.append((path, packed, row, ext))

    result.linked = [p for _, p, _, _ in linked]
    if not chosen:
        return result

    source = f"UARB {matter} {tab.value}"
    manifest = io.StringIO()
    fields = MANIFEST_FIELDS + [LINK_FIELD] if linked else MANIFEST_FIELDS
    w = csv.DictWriter(manifest, fieldnames=fields, lineterminator="\r\n", extrasaction="ignore")
    w.writeheader()
    for _, p, row, ext in chosen + linked:
        w.writerow(
            {
                "file": p.arcname,
                "doc_no": p.doc_no,
                "title": p.title,
                "date": row.date.isoformat() if row and row.date else "",
                "ext": ext,
                "security": (row.security if row else None) or "",
                "size": p.size,
                "sha256": p.sha256,
                "source": source,
                LINK_FIELD: p.url or "",
            }
        )
    readme = _readme(matter, tab, chosen, result.linked, result.skipped, retrieved_at, summary)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / zip_name(matter, tab)
    stamp = retrieved_at.astimezone(dt.UTC).timetuple()[:6]
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".tmp-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(zipfile.ZipInfo("README.txt", stamp), readme, compress_type=zipfile.ZIP_DEFLATED)
            # utf-8-sig so Excel reads accented titles correctly
            manifest_bytes = manifest.getvalue().encode("utf-8-sig")
            zf.writestr(zipfile.ZipInfo("manifest.csv", stamp), manifest_bytes, compress_type=zipfile.ZIP_DEFLATED)
            for path, p, _, _ in chosen:
                zf.write(path, p.arcname)
        with zipfile.ZipFile(tmp) as zf:
            if bad := zf.testzip():
                raise zipfile.BadZipFile(f"corrupt entry after writing: {bad}")
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    result.path = dest
    result.included = [p for _, p, _, _ in chosen]
    return result


def _readme(
    matter: str,
    tab: DocType,
    chosen: list[tuple[Path, PackedFile, DocRow | None, str]],
    linked: list[PackedFile],
    skipped: list[Skipped],
    retrieved_at: dt.datetime,
    summary: str | None,
) -> str:
    lines = [f"{matter} - {tab.value}", ""]
    if summary:
        lines += [summary.strip(), ""]
    lines += [
        f"{len(chosen)} file(s) retrieved from the Nova Scotia UARB public document database",
        f"(https://uarb.novascotia.ca) on {retrieved_at.astimezone(dt.UTC):%Y-%m-%d %H:%M} UTC.",
        "manifest.csv lists each file with its document number, date and SHA-256 checksum.",
    ]
    if linked:
        lines += ["", "Too large to email, sent as download links (see the link column in manifest.csv):"]
        lines += [f"  - {p.doc_no} {p.title}: {p.url}" for p in linked]
    if skipped:
        lines += ["", "Not included:"]
        lines += [f"  - {s.doc_no} {s.title}: {s.reason}" for s in skipped]
    return "\r\n".join(lines) + "\r\n"
