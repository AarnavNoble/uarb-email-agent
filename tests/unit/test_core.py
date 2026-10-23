import zipfile

import pytest

from tests.fake_site import COUNTS, FakeSite, make_rows
from uarb_agent.cache.store import Store
from uarb_agent.config import Settings
from uarb_agent.core import CONFIDENTIAL_REASON, Fetcher, as_error
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
