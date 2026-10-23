import csv
import datetime as dt
import hashlib
import io
import itertools
import zipfile

import pytest

from uarb_agent.models import DocRow, DocType, FileResult
from uarb_agent.pack.zipper import MB, build_zip, entry_name, zip_name

OTHER = DocType.OTHER_DOCUMENTS
WHEN = dt.datetime(2026, 10, 23, 14, 30, tzinfo=dt.UTC)
_n = itertools.count()


def _ok(tmp_path, doc_no, title, data: bytes, ext=".pdf", **kw) -> FileResult:
    p = tmp_path / "dl" / f"{next(_n)}{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return FileResult(doc_no=doc_no, title=title, path=p, size=len(data), ok=True, **kw)


def _sparse(tmp_path, doc_no, size) -> FileResult:
    p = tmp_path / "dl" / f"{doc_no}.pdf"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        f.truncate(size)
    return FileResult(doc_no=doc_no, title=f"Doc {doc_no}", path=p, size=size, ok=True)


def _read(result):
    with zipfile.ZipFile(result.path) as zf:
        names = zf.namelist()
        manifest = list(csv.DictReader(io.StringIO(zf.read("manifest.csv").decode("utf-8-sig"))))
        readme = zf.read("README.txt").decode()
        blobs = {n: zf.read(n) for n in names if n not in ("manifest.csv", "README.txt")}
        assert not any(i.flag_bits & 0x1 for i in zf.infolist())
    return names, manifest, readme, blobs


def test_zip_name():
    assert zip_name("M12205", OTHER) == "M12205_Other_Documents.zip"
    assert zip_name("M12205", DocType.KEY_DOCUMENTS) == "M12205_Key_Documents.zip"


def test_round_trip_manifest_matches_contents(tmp_path):
    rows = [
        DocRow(doc_no="312345", title="Evidence", date=dt.date(2025, 5, 1), ext=".pdf", security="Public", tab=OTHER),
        DocRow(doc_no="312346", title="Letter", date=None, ext=".docx", security=None, tab=OTHER),
    ]
    files = [_ok(tmp_path, "312345", "Evidence", b"%PDF a"), _ok(tmp_path, "312346", "Letter", b"docx", ext=".docx")]
    res = build_zip(tmp_path / "out", "M12205", OTHER, files, rows, retrieved_at=WHEN)

    assert res.path == tmp_path / "out" / "M12205_Other_Documents.zip"
    assert res.skipped == []
    names, manifest, readme, blobs = _read(res)
    assert names[:2] == ["README.txt", "manifest.csv"]
    assert sorted(blobs) == ["312345 - Evidence.pdf", "312346 - Letter.docx"]
    assert list(manifest[0]) == ["file", "doc_no", "title", "date", "ext", "security", "size", "sha256", "source"]
    first = manifest[0]
    assert first["date"] == "2025-05-01" and first["security"] == "Public" and first["ext"] == ".pdf"
    assert first["source"] == "UARB M12205 Other Documents"
    for m in manifest:
        data = blobs[m["file"]]
        assert hashlib.sha256(data).hexdigest() == m["sha256"]
        assert int(m["size"]) == len(data)
    assert manifest[1]["date"] == "" and manifest[1]["security"] == ""
    assert "M12205 - Other Documents" in readme and "2026-10-23 14:30 UTC" in readme
    assert not list((tmp_path / "out").glob(".tmp-*"))


def test_budget_overflow_skips_then_fills_with_smaller(tmp_path):
    files = [_sparse(tmp_path, "1", 8 * MB), _sparse(tmp_path, "2", 8 * MB), _sparse(tmp_path, "3", 5 * MB)]
    files.append(_sparse(tmp_path, "4", 1 * MB))
    res = build_zip(tmp_path / "out", "M12205", OTHER, files, retrieved_at=WHEN)

    assert [p.doc_no for p in res.included] == ["1", "2", "4"]
    assert [s.doc_no for s in res.skipped] == ["3"]
    assert "budget" in res.skipped[0].reason
    assert res.raw_bytes <= 18 * MB
    _, manifest, readme, _ = _read(res)
    assert [m["doc_no"] for m in manifest] == ["1", "2", "4"]
    assert "Not included" in readme and "Doc 3" in readme


def test_single_file_larger_than_budget(tmp_path):
    res = build_zip(tmp_path / "out", "M12205", OTHER, [_sparse(tmp_path, "1", 20 * MB)], retrieved_at=WHEN)
    assert res.path is None and res.included == []
    assert "too large" in res.skipped[0].reason and "20.0 MB" in res.skipped[0].reason
    assert not (tmp_path / "out" / "M12205_Other_Documents.zip").exists()


def test_custom_budget(tmp_path):
    files = [_ok(tmp_path, "1", "a", b"x" * 200_000), _ok(tmp_path, "2", "b", b"y" * 100)]
    res = build_zip(tmp_path / "out", "M12205", OTHER, files, budget_bytes=200_000, retrieved_at=WHEN)
    assert [p.doc_no for p in res.included] == ["2"]


@pytest.mark.parametrize(
    ("doc_no", "title", "ext", "expected"),
    [
        ("312345", "Évaluation du réseau – Phase 2", ".PDF", "312345 - Evaluation du reseau Phase 2.pdf"),
        ("H-4(C)-i", 'Reply: "IR" responses / NSPI?', ".pdf", "H-4(C)-i - Reply IR responses NSPI.pdf"),
        ("1", "  ...  ", ".pdf", "1.pdf"),
        ("1", "Tab\tand\nnewline\x00", ".xlsx", "1 - Tab and newline.xlsx"),
        ("a/b", "x", ".pdf", "ab - x.pdf"),
        ("", "日本語", "", "doc"),
    ],
)
def test_entry_name_is_safe_ascii(doc_no, title, ext, expected):
    assert entry_name(doc_no, title, ext) == expected


def test_long_title_truncated_on_word_boundary():
    name = entry_name("1", "word " * 40, ".pdf")
    assert name.endswith("word.pdf") and len(name) <= 90


def test_duplicate_names_are_deduped(tmp_path):
    files = [
        _ok(tmp_path, "7", "Letter", b"a"),
        _ok(tmp_path, "7", "Letter", b"b"),  # multi-file modal: same doc twice
        _ok(tmp_path, "7", "LETTER", b"c"),
    ]
    res = build_zip(tmp_path / "out", "M12205", OTHER, files, retrieved_at=WHEN)
    names = [p.arcname for p in res.included]
    assert names == ["7 - Letter.pdf", "7 - Letter (2).pdf", "7 - LETTER (3).pdf"]
    _, manifest, _, blobs = _read(res)
    assert len(blobs) == 3 and [m["file"] for m in manifest] == names


def test_title_cannot_shadow_manifest(tmp_path):
    files = [_ok(tmp_path, "1", "x", b"a")]
    res = build_zip(tmp_path / "out", "M12205", OTHER, files, retrieved_at=WHEN)
    assert "manifest.csv" not in [p.arcname for p in res.included]


def test_blocked_and_opaque_extensions_dropped(tmp_path):
    files = [
        _ok(tmp_path, "1", "Setup", b"MZ", ext=".exe"),
        _ok(tmp_path, "2", "Script", b"x", ext=".JS"),
        _ok(tmp_path, "3", "Bundle", b"x", ext=".rar"),
        _ok(tmp_path, "4", "Fine", b"x", ext=".pdf"),
    ]
    res = build_zip(tmp_path / "out", "M12205", OTHER, files, retrieved_at=WHEN)
    assert [p.doc_no for p in res.included] == ["4"]
    reasons = {s.doc_no: s.reason for s in res.skipped}
    assert "Gmail" in reasons["1"] and "Gmail" in reasons["2"] and "Gmail" in reasons["3"]


def test_nested_zip_checked(tmp_path):
    def mk(name, inner_name):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(inner_name, b"x")
        return _ok(tmp_path, name, f"Archive {name}", buf.getvalue(), ext=".zip")

    files = [mk("1", "data.xlsx"), mk("2", "run.bat"), _ok(tmp_path, "3", "Broken", b"nope", ext=".zip")]
    res = build_zip(tmp_path / "out", "M12205", OTHER, files, retrieved_at=WHEN)
    assert [p.doc_no for p in res.included] == ["1"]
    assert {s.doc_no for s in res.skipped} == {"2", "3"}


def test_failed_missing_and_tampered_files_reported(tmp_path):
    good = _ok(tmp_path, "1", "Good", b"good")
    gone = _ok(tmp_path, "2", "Gone", b"x")
    gone.path.unlink()
    tampered = _ok(tmp_path, "3", "Tampered", b"x", sha256="0" * 64)
    failed = FileResult(doc_no="4", title="Timeout", ok=False, error="timeout after 20 s")
    res = build_zip(tmp_path / "out", "M12205", OTHER, [good, gone, tampered, failed], retrieved_at=WHEN)
    assert [p.doc_no for p in res.included] == ["1"]
    reasons = {s.doc_no: s.reason for s in res.skipped}
    assert "missing" in reasons["2"]
    assert "checksum" in reasons["3"]
    assert reasons["4"] == "download failed: timeout after 20 s"


def test_nothing_to_pack(tmp_path):
    res = build_zip(tmp_path / "out", "M12205", OTHER, [], retrieved_at=WHEN)
    assert res.path is None and res.included == [] and res.skipped == []


def test_summary_in_readme_and_rebuild_overwrites(tmp_path):
    f = [_ok(tmp_path, "1", "a", b"a")]
    build_zip(tmp_path / "out", "M12205", OTHER, f, retrieved_at=WHEN)
    res = build_zip(tmp_path / "out", "M12205", OTHER, f, retrieved_at=WHEN, summary="M12205 is about rates.")
    _, _, readme, _ = _read(res)
    assert "M12205 is about rates." in readme
    assert len(list((tmp_path / "out").iterdir())) == 1


def _fake_publish(published: list):
    def publish(path, filename):
        published.append((path, filename))
        return f"https://example.test/d/tok/{filename}"

    return publish


def test_oversized_files_published_as_links(tmp_path):
    files = [
        _sparse(tmp_path, "1", 20 * MB),
        _sparse(tmp_path, "2", 1 * MB),
        _sparse(tmp_path, "3", 17 * MB),
    ]  # 3: over budget
    published: list = []
    res = build_zip(tmp_path / "out", "M12205", OTHER, files, retrieved_at=WHEN, publish=_fake_publish(published))

    assert [p.doc_no for p in res.included] == ["2"]
    assert [(p.doc_no, p.url) for p in res.linked] == [
        ("1", "https://example.test/d/tok/1 - Doc 1.pdf"),
        ("3", "https://example.test/d/tok/3 - Doc 3.pdf"),
    ]
    assert res.skipped == []
    assert [name for _, name in published] == ["1 - Doc 1.pdf", "3 - Doc 3.pdf"]
    names, manifest, readme, _ = _read(res)
    assert "1 - Doc 1.pdf" not in names
    assert list(manifest[0])[-1] == "link"
    assert [(m["doc_no"], m["link"]) for m in manifest] == [
        ("2", ""),
        ("1", "https://example.test/d/tok/1 - Doc 1.pdf"),
        ("3", "https://example.test/d/tok/3 - Doc 3.pdf"),
    ]
    assert manifest[1]["size"] == str(20 * MB) and len(manifest[1]["sha256"]) == 64
    assert "download links" in readme and "https://example.test/d/tok/3 - Doc 3.pdf" in readme


def test_all_oversized_linked_means_no_zip(tmp_path):
    res = build_zip(
        tmp_path / "out",
        "M12205",
        OTHER,
        [_sparse(tmp_path, "1", 20 * MB)],
        retrieved_at=WHEN,
        publish=_fake_publish([]),
    )
    assert res.path is None and res.included == [] and res.skipped == []
    assert [p.doc_no for p in res.linked] == ["1"]


def test_publish_failure_falls_back_to_skip(tmp_path):
    def broken(path, filename):
        raise OSError("disk full")

    res = build_zip(
        tmp_path / "out", "M12205", OTHER, [_sparse(tmp_path, "1", 20 * MB)], retrieved_at=WHEN, publish=broken
    )
    assert res.linked == [] and "too large" in res.skipped[0].reason
