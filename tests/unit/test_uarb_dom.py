"""The connector's page-side code against the recon HTML in a local Chromium. Every request is aborted,
so nothing leaves the machine; geometry is not checked here (the site's CSS is not in the fixtures)."""

from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from uarb_agent.config import Settings
from uarb_agent.models import DocType, InvalidMatterNumber, SiteUnavailable
from uarb_agent.uarb.connector import Connector

FIX = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
async def connector(tmp_path):
    c = Connector(Settings(data_dir=tmp_path, uarb_session_gap_s=0))
    yield c
    await c.close()


async def load(s, name: str):
    await s.page.route("**/*", lambda route: route.abort())
    await s.page.set_content((FIX / name).read_text(), wait_until="domcontentloaded")


async def test_counts_and_no_modal_on_matter_page(connector):
    async with connector.session() as s:
        await load(s, "matter.html")
        counts = await s.read_counts()
        assert counts[DocType.OTHER_DOCUMENTS] == 43 and counts[DocType.TRANSCRIPTS] == 0
        assert await s.read_modal() is None


async def test_download_dialog_is_read(connector):
    async with connector.session() as s:
        await load(s, "modal_0.html")
        text = await s.read_modal()
        assert text.startswith("Download Files") and "102674.pdf" in text
        assert await s.page.locator(".v-window .fm-download-button").count() == 1


async def test_session_maps_playwright_failure_and_saves_trace(connector, tmp_path):
    with pytest.raises(SiteUnavailable) as info:
        async with connector.session(trace_id="t-1") as s:
            await load(s, "matter.html")
            raise PlaywrightTimeout("Timeout 15000ms exceeded.")
    assert info.value.trace_id == "t-1"
    trace = tmp_path / "traces" / "t-1"
    assert {p.name for p in trace.iterdir()} == {"error.png", "error.html", "error.txt"}
    assert "Other Documents - 43" in (trace / "error.html").read_text()


async def test_bad_matter_number_fails_before_navigation(connector):
    with pytest.raises(InvalidMatterNumber) as info:
        async with connector.session() as s:
            await s.page.route("**/*", lambda route: route.abort())
            await s.open_matter("M1234")
    assert info.value.trace_id
    assert s.page.url == "about:blank"
