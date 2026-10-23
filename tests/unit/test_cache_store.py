import datetime as dt

from uarb_agent.cache.store import CachedFile, Store, sha256_file
from uarb_agent.models import DocRow, DocType, MatterMetadata

OTHER = DocType.OTHER_DOCUMENTS


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _file(tmp_path, name, data: bytes):
    p = tmp_path / "dl" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_doc_round_trip_and_content_addressing(tmp_path):
    store = Store(tmp_path / "cache")
    src = _file(tmp_path, "312345.pdf", b"%PDF-1.7 hello")
    [cf] = store.put_doc("M12205", OTHER, "312345", [src])

    assert cf.sha256 == sha256_file(src)
    assert cf.path.name == f"{cf.sha256}.pdf"
    assert cf.path.read_bytes() == b"%PDF-1.7 hello"
    assert src.exists()  # caller's temp file is left alone
    assert store.get_doc("M12205", OTHER, "312345") == [cf]
    assert store.get_doc("M12205", OTHER, "999") is None
    assert store.get_doc("M12205", DocType.EXHIBITS, "312345") is None


def test_identical_bytes_share_one_blob(tmp_path):
    store = Store(tmp_path / "cache")
    a = store.put_doc("M12205", OTHER, "1", [_file(tmp_path, "a.pdf", b"same")])
    b = store.put_doc("M12205", OTHER, "2", [_file(tmp_path, "b.pdf", b"same")])
    assert a[0].path == b[0].path
    assert len(list((tmp_path / "cache" / "blobs").rglob("*.pdf"))) == 1


def test_multi_file_doc_keeps_offered_names(tmp_path):
    store = Store(tmp_path / "cache")
    files = [(_file(tmp_path, "t1", b"one"), "part1.pdf"), (_file(tmp_path, "t2", b"two"), "part2.xlsx")]
    got = store.put_doc("M12205", DocType.EXHIBITS, "H-4(C)-i", files)
    assert [f.name for f in got] == ["part1.pdf", "part2.xlsx"]
    assert store.get_doc("M12205", DocType.EXHIBITS, "H-4(C)-i") == got


def test_weird_doc_numbers_do_not_collide(tmp_path):
    store = Store(tmp_path / "cache")
    store.put_doc("M12205", OTHER, "a/b", [_file(tmp_path, "x.pdf", b"x")])
    store.put_doc("M12205", OTHER, "a_b", [_file(tmp_path, "y.pdf", b"y")])
    assert store.get_doc("M12205", OTHER, "a/b")[0].path.read_bytes() == b"x"
    assert store.get_doc("M12205", OTHER, "a_b")[0].path.read_bytes() == b"y"
    assert not (tmp_path / "cache" / "docs" / "M12205" / "Other_Documents" / "a").exists()


def test_missing_or_truncated_blob_is_a_miss(tmp_path):
    store = Store(tmp_path / "cache")
    [cf] = store.put_doc("M12205", OTHER, "1", [_file(tmp_path, "a.pdf", b"abcdef")])
    cf.path.write_bytes(b"abc")
    assert store.get_doc("M12205", OTHER, "1") is None
    cf.path.unlink()
    assert store.get_doc("M12205", OTHER, "1") is None


def test_corrupt_record_is_a_miss(tmp_path):
    store = Store(tmp_path / "cache")
    store.put_doc("M12205", OTHER, "1", [_file(tmp_path, "a.pdf", b"a")])
    rec = next((tmp_path / "cache" / "docs").rglob("1.json"))
    rec.write_text("{not json")
    assert store.get_doc("M12205", OTHER, "1") is None


def test_no_temp_files_left_behind(tmp_path):
    store = Store(tmp_path / "cache")
    store.put_doc("M12205", OTHER, "1", [_file(tmp_path, "a.pdf", b"a")])
    store.put_rows("M12205", OTHER, [])
    assert not [p for p in (tmp_path / "cache").rglob(".tmp-*")]


META = MatterMetadata(
    matter_no="M12205", title="Some title", type="Electricity", category="Rates", date_received=dt.date(2025, 4, 7)
)
COUNTS = {DocType.EXHIBITS: 13, DocType.KEY_DOCUMENTS: 6, OTHER: 43, DocType.TRANSCRIPTS: 0, DocType.RECORDINGS: 0}


def test_matter_listing_ttl(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "cache", clock=clock)
    assert store.get_matter("M12205") is None
    store.put_matter("M12205", META, COUNTS)
    clock.t += 599
    assert store.get_matter("M12205") == (META, COUNTS)
    clock.t += 2
    assert store.get_matter("M12205") is None


def test_rows_listing_ttl_and_invalidate(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "cache", clock=clock)
    rows = [DocRow(doc_no="H-4(C)-i", title="Évidence", date=dt.date(2025, 5, 1), ext=".pdf", tab=OTHER)]
    store.put_rows("M12205", OTHER, rows)
    assert store.get_rows("M12205", OTHER) == rows
    assert store.get_rows("M12205", DocType.EXHIBITS) is None
    store.invalidate("M12205")
    assert store.get_rows("M12205", OTHER) is None
    store.put_rows("M12205", OTHER, rows)
    clock.t += 600
    assert store.get_rows("M12205", OTHER) is None


def test_documents_survive_listing_expiry(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "cache", clock=clock)
    store.put_doc("M12205", OTHER, "1", [_file(tmp_path, "a.pdf", b"a")])
    clock.t += 10 * 365 * 86400
    store.invalidate("M12205")
    assert isinstance(store.get_doc("M12205", OTHER, "1")[0], CachedFile)
