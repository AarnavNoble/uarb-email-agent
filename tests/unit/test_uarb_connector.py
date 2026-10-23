"""Connector control flow with fakes: no browser, no network."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from uarb_agent.models import DocRow, DocType, FileResult, UnexpectedModal
from uarb_agent.uarb.browser import Browser
from uarb_agent.uarb.connector import UarbSession, safe_filename
from uarb_agent.uarb.settle import settle

GRID_OTHER = json.loads((Path(__file__).parents[1] / "fixtures" / "uarb" / "grid_other.json").read_text())


class TextPage:
    """Just enough Page for settle(): innerText follows a script, then stays on its last value."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    async def evaluate(self, _js):
        self.calls += 1
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(ms / 1000)


async def test_settle_waits_for_stable_text():
    page = TextPage(["a", "ab", "abc", "abc"])
    assert await settle(page, quiet_ms=50, poll_ms=10) == "abc"
    assert page.calls >= 4


async def test_settle_gives_up_with_last_text():
    page = TextPage([str(i) for i in range(10_000)])
    start = time.monotonic()
    text = await settle(page, quiet_ms=500, timeout_s=0.2, poll_ms=10)
    assert time.monotonic() - start < 1 and text.isdigit()


# --- browser pool -----------------------------------------------------------------------------------


class FakeContext:
    def __init__(self, stats):
        self.stats = stats
        stats["open"] += 1
        stats["peak"] = max(stats["peak"], stats["open"])
        stats["starts"].append(time.monotonic())

    def set_default_timeout(self, _ms):
        pass

    async def new_page(self):
        return object()

    async def close(self):
        self.stats["open"] -= 1
        self.stats["closed"] += 1


class FakeBrowser:
    def __init__(self):
        self.stats = {"open": 0, "peak": 0, "closed": 0, "starts": []}

    async def new_context(self, **_kw):
        return FakeContext(self.stats)


def pool(concurrency=2, gap=0.0):
    b = Browser(concurrency=concurrency, min_gap_s=gap)
    fake = FakeBrowser()

    async def ensure():
        return fake

    b._ensure = ensure
    return b, fake.stats


async def test_pool_caps_concurrent_sessions():
    b, stats = pool(concurrency=2)

    async def job():
        async with b.page():
            await asyncio.sleep(0.05)

    await asyncio.gather(*(job() for _ in range(5)))
    assert stats["peak"] == 2 and stats["closed"] == 5 and stats["open"] == 0


async def test_pool_spaces_session_starts():
    b, stats = pool(concurrency=2, gap=0.1)

    async def job():
        async with b.page():
            pass

    await asyncio.gather(*(job() for _ in range(3)))
    gaps = [b - a for a, b in zip(stats["starts"], stats["starts"][1:], strict=False)]
    assert all(g >= 0.09 for g in gaps)


async def test_pool_closes_context_on_error():
    b, stats = pool()
    with pytest.raises(RuntimeError):
        async with b.page():
            raise RuntimeError("boom")
    assert stats["closed"] == 1 and stats["open"] == 0


async def test_pool_relaunches_after_driver_death():
    """A dead Playwright driver makes every new_context fail; the pool must start a fresh one, not stay broken."""
    b = Browser(min_gap_s=0)

    class Dead:
        async def new_context(self, **_kw):
            raise Exception("Browser.new_context: Connection closed while reading from the driver")

        async def close(self):
            pass

    dead, live = Dead(), FakeBrowser()
    b._browser = dead
    closes = []

    async def ensure():
        return b._browser or live

    async def close():
        closes.append(1)
        b._browser = None

    b._ensure, b.close = ensure, close
    async with b.page():
        pass
    assert closes == [1] and live.stats["closed"] == 1


# --- session logic with the page faked out ----------------------------------------------------------


def session(tmp_path: Path) -> UarbSession:
    s = UarbSession(None, url="https://example.invalid", trace_dir=tmp_path / "trace")
    s.matter = "M12205"

    async def no_trace(*_a, **_k):
        return None

    async def no_modal():
        return None

    s.save_trace = no_trace
    s.read_modal = no_modal
    return s


def replay_grid(s: UarbSession, snapshots):
    """Serve captured grid snapshots in order, advancing one per scroll."""
    state = {"i": 0, "scrolls": 0}

    async def grid():
        return snapshots[min(state["i"], len(snapshots) - 1)]

    async def scroll(_js, _arg=None):
        state["scrolls"] += 1
        state["i"] += 1

    s._grid, s._scroll = grid, scroll
    return state


async def test_collect_rows_scrolls_until_found_count(tmp_path):
    s = session(tmp_path)
    state = replay_grid(s, GRID_OTHER)
    rows = await s._collect_rows(DocType.OTHER_DOCUMENTS, 43)
    assert len(rows) == 43 == len({r.doc_no for r in rows})
    assert state["scrolls"] == len(GRID_OTHER)  # last scroll is back to the top


async def test_collect_rows_stops_when_scrolling_adds_nothing(tmp_path):
    s = session(tmp_path)
    stuck = {**GRID_OTHER[0], "at_bottom": False}
    state = replay_grid(s, [stuck])
    rows = await s._collect_rows(DocType.OTHER_DOCUMENTS, 43)
    assert len(rows) == len(stuck["rows"]) < 43
    assert state["scrolls"] == 4  # three fruitless scrolls, then back to top


async def test_zero_count_tab_is_never_clicked(tmp_path):
    s = session(tmp_path)

    async def boom(_tab):
        raise AssertionError("tab clicked")

    s._open_tab = boom
    assert await s.list_documents(DocType.TRANSCRIPTS, count=0) == []
    s.counts = {t: 0 for t in DocType}
    assert await s.list_documents(DocType.RECORDINGS) == []


ROW = DocRow(doc_no="102674", title="Board Order", tab=DocType.OTHER_DOCUMENTS)


async def test_download_retries_a_transient_failure_once(tmp_path):
    s = session(tmp_path)
    calls = []

    async def flaky(row, dest):
        calls.append(row.doc_no)
        if len(calls) == 1:
            raise PlaywrightTimeout("Timeout 20000ms exceeded")
        return [FileResult(doc_no=row.doc_no, title=row.title, ok=True)]

    s._try_download = flaky
    files = await s.download([ROW], tmp_path / "dl")
    assert calls == ["102674", "102674"] and files[0].ok


async def test_download_gives_up_after_second_failure_and_continues(tmp_path):
    s = session(tmp_path)
    other = ROW.model_copy(update={"doc_no": "102454"})

    async def fails_first_row(row, dest):
        if row.doc_no == "102674":
            raise PlaywrightTimeout("Timeout 20000ms exceeded")
        return [FileResult(doc_no=row.doc_no, title=row.title, ok=True)]

    s._try_download = fails_first_row
    files = await s.download([ROW, other], tmp_path / "dl")
    assert [(f.doc_no, f.ok) for f in files] == [("102674", False), ("102454", True)]
    assert "TimeoutError" in files[0].error


async def test_download_does_not_retry_a_refusal(tmp_path):
    s = session(tmp_path)
    calls = []

    async def refused(row, dest):
        calls.append(row.doc_no)
        raise UnexpectedModal("Access denied | OK")

    s._try_download = refused
    files = await s.download([ROW], tmp_path / "dl")
    assert len(calls) == 1 and not files[0].ok and "Access denied" in files[0].error


def test_safe_filename():
    assert safe_filename("102674.pdf") == "102674.pdf"
    assert safe_filename("../H-4(C)-i report.pdf") == "H-4(C)-i_report.pdf"


async def test_trace_on_a_hung_page_gives_up_quickly_and_keeps_the_error(tmp_path, monkeypatch):
    # Regression: a hung site made the trace screenshot wait 10 s, twice per fetch, past the request budget.
    monkeypatch.setattr("uarb_agent.uarb.connector.TRACE_TIMEOUT_S", 0.05)

    class HungPage:
        async def screenshot(self, *, path, **kw):
            assert kw["timeout"] == 50
            raise PlaywrightTimeout("screenshot timed out")

        async def content(self):
            await asyncio.sleep(60)

    s = UarbSession(HungPage(), url="https://example.invalid", trace_dir=tmp_path / "trace")
    start = time.monotonic()
    assert await s.save_trace("error", RuntimeError("goto timed out")) is None
    assert time.monotonic() - start < 1
    assert "goto timed out" in (tmp_path / "trace" / "error.txt").read_text()

    class SlowContentPage(HungPage):
        async def screenshot(self, *, path, **_kw):
            path.write_bytes(b"png")

    start = time.monotonic()
    assert await UarbSession(SlowContentPage(), url="", trace_dir=tmp_path / "t2").save_trace("x") is None
    assert time.monotonic() - start < 1
