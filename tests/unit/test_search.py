import datetime as dt
from pathlib import Path

import pymupdf
import pytest

from uarb_agent.cache.store import Store
from uarb_agent.config import load_settings
from uarb_agent.models import DocRow, DocType, FileResult, MatterMetadata, MatterReport
from uarb_agent.search import SearchIndex, format_hits, fts_query, run_index, run_search
from uarb_agent.search.index import MAX_TERMS, IndexStats

KEY = DocType.KEY_DOCUMENTS
OTHER = DocType.OTHER_DOCUMENTS
REAL_CACHE = Path(__file__).resolve().parents[2] / "data" / "cache"

ORDER_P1 = "ORDER. In its Decision dated October 23, 2025, the Board approved the project with the changes directed."
ORDER_P2 = "Halifax Water is directed to continue to file semi-annual (every six months) reports on the project."
DECISION_P1 = "The Board directs that progress reports must be filed annually, for a total project cost of $69,275,000."


def make_pdf(path: Path, pages: list[str | None]) -> Path:
    """One page per item; None makes an image-only page (a scan with no text layer)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text is None:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
            pix.clear_with(180)
            page.insert_image(pymupdf.Rect(72, 72, 300, 300), pixmap=pix)
        else:
            page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), text, fontsize=11)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def idx(tmp_path):
    with SearchIndex(tmp_path / "search.sqlite3") as index:
        yield index


@pytest.fixture
def order_pdf(tmp_path):
    return make_pdf(tmp_path / "src" / "102674.pdf", [ORDER_P1, ORDER_P2, None])


# query sanitising


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("semi-annual reports", '"semi annual" "reports"'),
        ('"total project cost" approved', '"total project cost" "approved"'),
        ("rate AND NOT (base) NEAR*", '"rate" "AND" "NOT" "base" "NEAR"'),
        ('unbalanced "quote here', '"unbalanced" "quote" "here"'),
        ("rate rate", '"rate"'),
        ("  -- ** () ", None),
        ("", None),
    ],
)
def test_fts_query_quotes_every_term(text, expected):
    assert fts_query(text) == expected


def test_fts_query_keeps_at_most_max_terms():
    q = fts_query(" ".join(f"w{i}" for i in range(5000)))
    assert q.count('"') == 2 * MAX_TERMS and q.startswith('"w0" "w1"')


def test_fts_query_any_term_joins_with_or():
    assert fts_query("semi-annual reports", any_term=True) == '"semi annual" OR "reports"'


@pytest.mark.parametrize(
    "text", ['"', "''", "a:b", "col:rate", "NEAR(a b, 2)", "^start", "x*", "-annual", "AND", "(", "{}", "\\"]
)
def test_search_never_raises_on_odd_input(idx, order_pdf, text):
    idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf, title="Board Order")
    assert isinstance(idx.search(text), list)


# indexing and searching


def test_hit_cites_document_and_page(idx, order_pdf):
    added = idx.index_document(
        matter="M12205", tab=KEY, doc_no="102674", path=order_pdf, title="Board Order", date=dt.date(2026, 7, 8)
    )
    assert added == 3

    [hit] = idx.search("semi-annual reports")
    assert (hit.matter, hit.tab, hit.doc_no, hit.title, hit.date, hit.page) == (
        "M12205",
        "Key Documents",
        "102674",
        "Board Order",
        "2026-07-08",
        2,
    )
    assert "**semi-annual**" in hit.snippet and "**reports**" in hit.snippet
    assert hit.all_terms and hit.cite() == "102674 p.2"


def test_porter_stemming_matches_word_forms(idx, order_pdf):
    idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf)
    assert [h.page for h in idx.search("approve")] == [1]


def test_reindexing_is_a_noop(idx, order_pdf):
    assert idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf) == 3
    assert idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf) == 0
    s = idx.stats()
    assert (s.documents, s.pages, s.image_only_pages, s.matters) == (1, 3, 1, 1)


def test_concurrent_indexing_of_one_file_adds_its_pages_once(idx, order_pdf, monkeypatch):
    import uarb_agent.search.index as mod

    real = mod.extract_pages
    nested: list[int] = []

    def racing(path):  # another worker thread finishes the same file while this one is still extracting
        monkeypatch.setattr(mod, "extract_pages", real)
        nested.append(idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf))
        return real(path)

    monkeypatch.setattr(mod, "extract_pages", racing)
    assert idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf) == 0
    assert nested == [3]
    assert idx._db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 3


def test_stats_count_a_document_listed_under_two_tabs_once(idx, order_pdf):
    for tab in (KEY, OTHER):
        idx.index_document(matter="M12205", tab=tab, doc_no="102674", path=order_pdf, title="Board Order")
    for s in (idx.stats(), idx.stats("M12205"), idx.stats("m12205")):
        assert (s.documents, s.pages, s.image_only_pages, s.matters, s.skipped) == (1, 3, 1, 1, 0)
    assert idx.stats("not a matter") == idx.stats("M99999") == IndexStats(0, 0, 0, 0, 0)


def test_later_title_and_date_fill_in_missing_ones(idx, order_pdf):
    idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf)
    assert idx.search("semi-annual")[0].title == ""
    idx.index_document(
        matter="M12205", tab=KEY, doc_no="102674", path=order_pdf, title="Board Order", date="2026-07-08"
    )
    hit = idx.search("semi-annual")[0]
    assert (hit.title, hit.date) == ("Board Order", "2026-07-08")
    assert idx.search('"board order"')  # the title itself is searchable


def test_image_only_pages_are_flagged_not_searched(idx, tmp_path):
    scan = make_pdf(tmp_path / "scan.pdf", [None, None])
    idx.index_document(matter="M12205", tab=OTHER, doc_no="102197", path=scan, title="Scanned letter")
    s = idx.stats()
    assert (s.pages, s.image_only_pages) == (2, 2)
    [hit] = idx.search("scanned letter")  # still found by title, once
    assert hit.snippet == "(title match)"


def test_matter_filter(idx, tmp_path, order_pdf):
    idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf)
    other = make_pdf(tmp_path / "86599.pdf", ["The Board directs that semi-annual progress reports must be filed."])
    idx.index_document(matter="M10431", tab=KEY, doc_no="86599", path=other)

    assert {h.matter for h in idx.search("semi-annual")} == {"M12205", "M10431"}
    assert [h.doc_no for h in idx.search("semi-annual", matter="m-10431")] == ["86599"]
    assert idx.search("semi-annual", matter="not a matter") == []


def test_one_hit_per_document_and_limit(idx, tmp_path):
    many = make_pdf(tmp_path / "many.pdf", [f"Page {i}: the rate base was approved again." for i in range(6)])
    idx.index_document(matter="M12205", tab=KEY, doc_no="1", path=many)
    idx.index_document(matter="M12205", tab=OTHER, doc_no="1", path=many)  # same doc listed under two tabs
    assert len(idx.search("rate base")) == 1
    assert len(idx.search("rate base", per_doc=3)) == 3
    assert len(idx.search("rate base", per_doc=10, limit=4)) == 4


def test_exact_phrase_ranks_above_scattered_terms(idx, tmp_path):
    scattered = make_pdf(
        tmp_path / "99761.pdf",
        ["Semi-annual meetings are held. Design reports, progress reports and cost reports are filed with the Board."],
    )
    exact = make_pdf(tmp_path / "102674.pdf", [f"{ORDER_P1} Halifax Water shall file semi-annual reports. {ORDER_P1}"])
    idx.index_document(matter="M12205", tab=KEY, doc_no="99761", path=scattered, title="Board Decision")
    idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=exact, title="Board Order")
    hits = idx.search("semi-annual reports")
    assert [h.doc_no for h in hits] == ["102674", "99761"]
    assert all(h.all_terms for h in hits)


def test_any_term_fallback_is_marked(idx, order_pdf):
    idx.index_document(matter="M12205", tab=KEY, doc_no="102674", path=order_pdf)
    hits = idx.search("semi-annual zebra")
    assert hits and not hits[0].all_terms
    assert idx.search("zebra") == []
    assert "No single page has every word" in format_hits(hits, "semi-annual zebra")


def test_non_pdf_and_damaged_files_are_recorded_not_indexed(idx, tmp_path):
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"ID3" + b"\0" * 100)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7 not really")
    assert idx.index_document(matter="M12600", tab=DocType.RECORDINGS, doc_no="R-1", path=mp3) == 0
    assert idx.index_document(matter="M12600", tab=OTHER, doc_no="2", path=bad) == 0
    assert idx.stats().skipped == 2


def test_index_report_uses_report_rows(idx, order_pdf):
    report = MatterReport(
        metadata=MatterMetadata(matter_no="M12205", title="Windsor Street Exchange", type="Capital", category="Water"),
        counts={KEY: 1},
        requested=KEY,
        rows=[DocRow(doc_no="102674", title="Board Order", date=dt.date(2026, 7, 8), tab=KEY)],
        files=[
            FileResult(doc_no="102674", title="Board Order", path=order_pdf, ok=True),
            FileResult(doc_no="999", title="Failed", error="download failed"),
        ],
        trace_id="t",
    )
    assert idx.index_report(report) == 3
    assert idx.index_report(report) == 0
    assert idx.search("semi-annual")[0].date == "2026-07-08"


# backfill from the cache


def test_backfill_from_store(idx, tmp_path, order_pdf):
    store = Store(tmp_path / "cache")
    store.put_doc("M12205", KEY, "102674", [order_pdf])
    store.put_rows("M12205", KEY, [DocRow(doc_no="102674", title="Board Order", date=dt.date(2026, 7, 8), tab=KEY)])
    mp3 = tmp_path / "src" / "T001.MP3"
    mp3.write_bytes(b"ID3" + b"\0" * 100)
    store.put_doc("M12600", DocType.RECORDINGS, "R-20260924-e209", [mp3])
    no_listing = make_pdf(tmp_path / "src" / "101621.pdf", ["Cumberland County correspondence re amendment."])
    store.put_doc("M12383", OTHER, "101621", [no_listing])

    assert idx.backfill(store) == 4
    assert idx.backfill(store) == 0
    s = idx.stats()
    assert (s.documents, s.matters, s.skipped) == (3, 3, 1)
    hit = idx.search("semi-annual")[0]
    assert (hit.matter, hit.tab, hit.title, hit.date) == ("M12205", "Key Documents", "Board Order", "2026-07-08")
    assert idx.search("cumberland")[0].tab == "Other Documents"


def test_backfill_finds_blobs_when_recorded_path_is_relative_elsewhere(idx, tmp_path, order_pdf):
    store = Store(tmp_path / "cache")
    store.put_doc("M12205", KEY, "102674", [order_pdf])
    [record] = (tmp_path / "cache" / "docs").rglob("*.json")
    record.write_text(record.read_text().replace(str(tmp_path / "cache"), "somewhere/else/cache"))
    assert idx.backfill(store) == 3


def test_cli_helpers(tmp_path, order_pdf):
    settings = load_settings({"DATA_DIR": str(tmp_path / "data")})
    Store(settings.cache_dir).put_doc("M12205", KEY, "102674", [order_pdf])

    out = run_index(settings)
    assert out.startswith("Indexed 3 new pages.") and "1 scanned pages have no text layer" in out
    assert run_index(settings).startswith("Indexed 0 new pages.")
    text = run_search("semi-annual", settings=settings)
    assert "[102674 p.2]" in text and "GET 102674 (M12205)" in text
    assert run_search('"', settings=settings).startswith("No pages")


@pytest.mark.skipif(not (REAL_CACHE / "docs").is_dir(), reason="no local document cache")
def test_backfill_real_cache_smoke(idx):
    idx.backfill(Store(REAL_CACHE))
    assert idx.stats().documents > 0
    for q in ("board order", "semi-annual reports", '"', "AND OR NOT"):
        idx.search(q)
