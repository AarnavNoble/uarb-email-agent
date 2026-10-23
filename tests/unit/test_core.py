import time
import urllib.parse
import zipfile
from contextlib import asynccontextmanager

import pymupdf
import pytest

from tests.fake_site import COUNTS, FakeSession, FakeSite, make_rows
from uarb_agent.cache.store import Store
from uarb_agent.config import Settings
from uarb_agent.core import (
    BOARD_ONLY_REASON,
    CONFIDENTIAL_REASON,
    NOTICE_REASON,
    Fetcher,
    as_error,
    is_confidentiality_notice,
    request_timeout_s,
)
from uarb_agent.models import DocType, InvalidMatterNumber, MatterNotFound, SiteUnavailable

OTHER = DocType.OTHER_DOCUMENTS


def fetcher_for(tmp_path, site: FakeSite, **settings) -> Fetcher:
    s = Settings(data_dir=tmp_path / "data", **settings)
    return Fetcher(s, connector=site, store=Store(s.cache_dir))


async def test_newest_first_limit_and_cache(tmp_path):
    site = FakeSite()
    c = fetcher_for(tmp_path, site)
    report = await c.fetch("m-12205", OTHER)
    assert report.metadata.matter_no == "M12205"
    assert report.counts == COUNTS
    assert len(report.rows) == 14
    assert [f.doc_no for f in report.downloaded] == [r.doc_no for r in report.rows[:10]]
    assert all(f.path and f.path.exists() and f.sha256 for f in report.downloaded)
    assert "total" in report.timings

    # Second run: everything from the cache, no session at all.
    site.opened = 0
    again = await c.fetch("M12205", OTHER)
    assert site.opened == 0
    assert [f.sha256 for f in again.downloaded] == [f.sha256 for f in report.downloaded]
    assert site.downloaded.count(report.rows[0].doc_no) == 1


async def test_confidential_rows_are_skipped_and_replaced(tmp_path):
    rows = {t: make_rows(t, n) for t, n in COUNTS.items()}
    rows[OTHER] = make_rows(OTHER, 14, confidential=frozenset({0, 3}))
    site = FakeSite(rows=rows)
    report = await fetcher_for(tmp_path, site).fetch("M12205", OTHER)
    assert len(report.downloaded) == 10
    assert [s.doc_no for s in report.skipped] == [rows[OTHER][0].doc_no, rows[OTHER][3].doc_no]
    assert all(s.reason == CONFIDENTIAL_REASON for s in report.skipped)
    assert rows[OTHER][0].doc_no not in site.downloaded
    assert report.downloaded[-1].doc_no == rows[OTHER][11].doc_no


async def test_failed_download_takes_the_next_row(tmp_path):
    site = FakeSite()
    bad = site.rows[OTHER][2].doc_no
    site.broken = {bad}
    report = await fetcher_for(tmp_path, site).fetch("M12205", OTHER)
    failed = [f for f in report.files if not f.ok]
    assert [f.doc_no for f in failed] == [bad]
    assert len(report.downloaded) == 10
    assert report.downloaded[-1].doc_no == site.rows[OTHER][10].doc_no


async def test_offset_limit_and_past_end(tmp_path):
    c = fetcher_for(tmp_path, FakeSite())
    more = await c.fetch("M12205", OTHER, limit=10, offset=10)
    assert [f.doc_no for f in more.downloaded] == [r.doc_no for r in more.rows[10:14]]
    past = await c.fetch("M12205", OTHER, offset=14)
    assert past.files == [] and past.offset == 14


async def test_empty_tab_and_summary_never_list(tmp_path):
    site = FakeSite()
    c = fetcher_for(tmp_path, site)
    empty = await c.fetch("M12205", DocType.TRANSCRIPTS)
    summary = await c.fetch("M12205", None)
    assert empty.rows == [] and empty.files == []
    assert summary.requested is None and summary.rows == []
    assert site.listed == []


async def test_errors(tmp_path):
    site = FakeSite()
    c = fetcher_for(tmp_path, site)
    with pytest.raises(InvalidMatterNumber):
        await c.fetch("M1234", OTHER)
    assert site.opened == 0
    with pytest.raises(MatterNotFound):
        await c.fetch("M99999", OTHER)
    site.down = True
    with pytest.raises(SiteUnavailable):
        await c.fetch("M12205", OTHER)
    assert site.opened == 3  # M99999 once, then the outage tried twice (one retry)


async def test_pack_moves_left_out_files_to_skipped(tmp_path):
    site = FakeSite(file_size=60_000)  # each fake file ~360 KB
    c = fetcher_for(tmp_path, site)
    report = await c.fetch("M12205", OTHER, limit=3)
    packed = c.pack(report, tmp_path / "out", budget_bytes=900_000)
    assert packed and packed.path
    with zipfile.ZipFile(packed.path) as zf:
        assert "manifest.csv" in zf.namelist()
    assert len(report.downloaded) == len(packed.included) == 2
    assert report.skipped and "budget" in report.skipped[-1].reason


async def test_pack_links_oversized_files_when_public_url_set(tmp_path):
    site = FakeSite(file_size=60_000)  # each fake file ~360 KB
    c = fetcher_for(tmp_path, site, public_base_url="https://agent.example")
    report = await c.fetch("M12205", OTHER, limit=3)
    packed = c.pack(report, tmp_path / "out", budget_bytes=900_000)
    assert packed and packed.path and len(packed.included) == 2
    assert len(report.downloaded) == 3 and not report.skipped
    [linked] = [f for f in report.downloaded if f.url]
    assert linked.url.startswith("https://agent.example/d/")
    token, name = linked.url.split("/")[-2:]
    assert (c.settings.links_dir / token).is_dir()
    assert [p.name for p in (c.settings.links_dir / token).iterdir()] == [urllib.parse.unquote(name)]


async def test_pack_all_oversized_linked_without_zip(tmp_path):
    site = FakeSite(file_size=60_000)
    c = fetcher_for(tmp_path, site, public_base_url="https://agent.example")
    report = await c.fetch("M12205", OTHER, limit=2)
    packed = c.pack(report, tmp_path / "out", budget_bytes=300_000)
    assert packed and packed.path is None
    assert len(report.downloaded) == 2 and all(f.url for f in report.downloaded) and not report.skipped


async def test_pack_skips_oversized_without_public_url(tmp_path):
    site = FakeSite(file_size=60_000)
    c = fetcher_for(tmp_path, site)
    report = await c.fetch("M12205", OTHER, limit=2)
    packed = c.pack(report, tmp_path / "out", budget_bytes=300_000)
    assert packed and packed.path is None
    assert report.downloaded == [] and all("too large" in s.reason for s in report.skipped)
    assert not c.settings.links_dir.exists()


async def test_find_doc(tmp_path):
    site = FakeSite()
    c = fetcher_for(tmp_path, site)
    target = site.rows[DocType.EXHIBITS][1]
    report = await c.find_doc("M12205", target.doc_no)
    assert report is not None and report.requested == DocType.EXHIBITS
    assert [f.doc_no for f in report.downloaded] == [target.doc_no]
    assert report.offset == 1
    assert await c.find_doc("M12205", "nope") is None


def test_as_error_keeps_or_assigns_trace():
    err = as_error(TimeoutError(), "t1")
    assert isinstance(err, SiteUnavailable) and err.trace_id == "t1"
    nf = MatterNotFound("M99999", trace_id="t0")
    assert as_error(nf, "t1") is nf and nf.trace_id == "t0"


async def test_slow_downloads_stop_at_the_budget_and_keep_what_landed(tmp_path):
    # Regression: Exhibits on M12205 run 40+ MB each. All ten were fetched as one batch and stored only at the
    # end, so the worker's timeout discarded every finished download and the reply blamed the site.
    site = FakeSite(download_delay=0.05)
    c = fetcher_for(tmp_path, site)
    c.download_budget_s = 0.12
    report = await c.fetch("M12205", OTHER)
    got = [f.doc_no for f in report.downloaded]
    assert 1 <= len(got) < 10
    assert got == [r.doc_no for r in report.rows[: len(got)]]
    assert c.store.get_doc("M12205", OTHER, got[0])  # stored as it landed, not at the end

    # The next fetch reuses those from the cache and carries on.
    c.download_budget_s = 60
    again = await c.fetch("M12205", OTHER)
    assert len(again.downloaded) == 10
    assert all(site.downloaded.count(d) == 1 for d in got)


async def test_rows_come_newest_first_whatever_the_site_order(tmp_path):
    # Regression: Key Documents and Exhibits on M12205 are listed oldest first, so "10 newest" (and WATCH's
    # "first N rows are the new ones") picked the oldest filings.
    rows = {t: make_rows(t, n) for t, n in COUNTS.items()}
    key = DocType.KEY_DOCUMENTS
    rows[key] = list(reversed(make_rows(key, 2)))  # oldest first, as the site shows it
    site = FakeSite(rows=rows)
    report = await fetcher_for(tmp_path, site).fetch("M12205", key, limit=1)
    assert [r.date for r in report.rows] == sorted((r.date for r in rows[key]), reverse=True)
    assert report.downloaded[0].doc_no == max(rows[key], key=lambda r: r.date).doc_no


async def test_board_only_and_other_non_public_levels_are_skipped(tmp_path):
    # Live: M12600 has 5 'Board Only' exhibits and M10431 has 8; the site serves a confidentiality notice for them.
    rows = {t: make_rows(t, n) for t, n in COUNTS.items()}
    other = make_rows(OTHER, 14)
    other[0] = other[0].model_copy(update={"security": "Board Only"})
    other[1] = other[1].model_copy(update={"security": "Sealed"})
    other[2] = other[2].model_copy(update={"security": None})  # hearing rows often have no level: public
    rows[OTHER] = other
    site = FakeSite(rows=rows)
    report = await fetcher_for(tmp_path, site).fetch("M12205", OTHER)
    reasons = {s.doc_no: s.reason for s in report.skipped}
    assert reasons == {other[0].doc_no: BOARD_ONLY_REASON, other[1].doc_no: reasons[other[1].doc_no]}
    assert "'Sealed'" in reasons[other[1].doc_no]
    assert other[0].doc_no not in site.downloaded and other[1].doc_no not in site.downloaded
    assert report.downloaded[0].doc_no == other[2].doc_no and len(report.downloaded) == 10


def _pdf(path, text: str) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_is_confidentiality_notice(tmp_path):
    _pdf(tmp_path / "n.pdf", "Document: 320036\n\nCONFIDENTIALITY NOTICE\n\nThe document you are attempting ...")
    _pdf(tmp_path / "d.pdf", "Board Order\n\nThis order concerns a confidentiality notice filed by ...")
    (tmp_path / "junk.pdf").write_bytes(b"%PDF-1.4 not really")
    assert is_confidentiality_notice(tmp_path / "n.pdf")
    assert not is_confidentiality_notice(tmp_path / "d.pdf")
    assert not is_confidentiality_notice(tmp_path / "junk.pdf")


async def test_a_downloaded_confidentiality_notice_is_skipped_not_delivered(tmp_path):
    # Safety net for a non-public row the listing marked Public: the notice must not count as the document.
    site = FakeSite()
    notice = site.rows[OTHER][1].doc_no

    class NoticeSession(FakeSession):
        async def download(self, rows, dest):
            out = await super().download(rows, dest)
            if rows[0].doc_no == notice:
                _pdf(out[0].path, "Document: 1\nCONFIDENTIALITY NOTICE\nThe document you are attempting ...")
            return out

    @asynccontextmanager
    async def session(trace_id=None):
        yield NoticeSession(site)

    site.session = session
    c = fetcher_for(tmp_path, site)
    report = await c.fetch("M12205", OTHER)
    assert [(s.doc_no, s.reason) for s in report.skipped] == [(notice, NOTICE_REASON)]
    assert notice not in [f.doc_no for f in report.files] and len(report.downloaded) == 10
    assert c.store.get_doc("M12205", OTHER, notice) is None


async def test_a_file_slower_than_the_budget_is_cut_off_mid_download(tmp_path):
    # Regression: the budget was only checked between files, so one 212 MB recording ran past the worker's
    # timeout and the whole request failed as 'site not responding'.
    site = FakeSite(download_delay=30)
    c = fetcher_for(tmp_path, site)
    c.download_budget_s = 0.1
    start = time.monotonic()
    report = await c.fetch("M12205", OTHER)
    assert time.monotonic() - start < 2
    # Nothing landed, so the cut-off row is reported (MORE moves past it instead of retrying it forever).
    [failed] = report.files
    assert failed.doc_no == report.rows[0].doc_no and not failed.ok and "did not finish" in failed.error


async def test_a_cut_off_file_after_others_is_left_for_more(tmp_path):
    site = FakeSite()
    c = fetcher_for(tmp_path, site)
    first = await c.fetch("M12205", OTHER, limit=2)  # cached now
    site.download_delay = 30
    c.download_budget_s = 0.1
    report = await c.fetch("M12205", OTHER)
    assert [f.doc_no for f in report.files] == [f.doc_no for f in first.downloaded]  # the slow third is unreported
    assert all(f.ok for f in report.files)


def test_hearing_tabs_get_a_longer_budget():
    c = Fetcher(Settings(), connector=FakeSite(), store=None)
    assert c.budget_s(DocType.RECORDINGS) >= 200 and c.budget_s(DocType.TRANSCRIPTS) > c.budget_s(OTHER)
    assert c.budget_s(None) == c.budget_s(OTHER) == c.download_budget_s
    assert request_timeout_s(OTHER) == 120  # what the worker has always allowed per request
    assert request_timeout_s(DocType.RECORDINGS) > c.budget_s(DocType.RECORDINGS)


def _pages_pdf(path, pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((36, 36), text, fontsize=8)
    doc.save(path)
    doc.close()


def _site_with_board_order() -> FakeSite:
    """M12205 with a real-looking Board Order as its newest Key Document."""
    from tests.unit.test_outcome import ORDER_12205

    site = FakeSite()
    key = DocType.KEY_DOCUMENTS
    site.rows[key][0] = site.rows[key][0].model_copy(update={"title": "Board Order"})
    order_no = site.rows[key][0].doc_no

    class OrderSession(FakeSession):
        async def download(self, rows, dest):
            out = await super().download(rows, dest)
            if rows[0].doc_no == order_no:
                _pages_pdf(out[0].path, ORDER_12205)
            return out

    @asynccontextmanager
    async def session(trace_id=None):
        yield OrderSession(site)

    site.session = session
    return site


async def test_fetched_documents_are_indexed_and_the_decision_is_read_from_the_cache(tmp_path):
    fetcher = fetcher_for(tmp_path, _site_with_board_order())
    key = await fetcher.fetch("M12205", DocType.KEY_DOCUMENTS)
    assert key.decision and key.decision.startswith("Board decision: approved $59,143,000 of $64,769,000 requested")
    assert "Board Order" in key.decision and "semi-annual" in key.decision

    hits = fetcher.index.search("semi-annual reports", matter="M12205")
    assert [(h.doc_no, h.page) for h in hits] == [(key.rows[0].doc_no, 2)]
    assert fetcher.index.stats("M12205").documents == 2

    # A later summary (or any other tab) gets the same line from the cache, without downloading anything.
    summary = await fetcher.fetch("M12205", None)
    assert summary.decision == key.decision
    other = await fetcher.fetch("M12205", OTHER, limit=1)
    assert other.decision == key.decision
    await fetcher.close()


async def test_no_board_order_in_the_cache_means_no_decision(tmp_path):
    fetcher = fetcher_for(tmp_path, FakeSite())
    assert (await fetcher.fetch("M12205", DocType.KEY_DOCUMENTS)).decision is None  # titles are not orders
    assert (await fetcher.fetch("M12205", None)).decision is None
    await fetcher.close()


async def test_the_decision_goes_into_the_zip_readme(tmp_path):
    fetcher = fetcher_for(tmp_path, _site_with_board_order())
    report = await fetcher.fetch("M12205", DocType.KEY_DOCUMENTS)
    packed = fetcher.pack(report, tmp_path / "out")
    with zipfile.ZipFile(packed.path) as zf:
        assert report.decision in zf.read("README.txt").decode()
    await fetcher.close()
