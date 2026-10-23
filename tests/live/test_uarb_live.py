"""Against the real UARB site. Run with `make live`. Only M12205 and M99999, a pause before each test,
and at most two small downloads. Assertions are structural: live counts change and are never hard-coded."""

import asyncio

import pytest

from uarb_agent.config import Settings
from uarb_agent.models import DOC_TYPES, DocType, MatterNotFound
from uarb_agent.uarb.connector import Connector

pytestmark = pytest.mark.live


@pytest.fixture
async def connector(tmp_path):
    await asyncio.sleep(5)  # be gentle with the server between tests
    c = Connector(Settings(data_dir=tmp_path))
    yield c
    await c.close()


async def test_m12205_metadata_counts_rows_and_two_downloads(connector, tmp_path):
    async with connector.session() as s:
        await s.open_matter("m-12205")
        meta = await s.read_metadata()
        counts = await s.read_counts()
        rows = await s.list_documents(DocType.OTHER_DOCUMENTS)
        public = [r for r in rows if r.security == "Public"][:2]
        files = await s.download(public, tmp_path / "dl")

    assert meta.matter_no == "M12205" and meta.title and meta.type and meta.category
    assert meta.date_received is not None
    assert list(counts) == list(DOC_TYPES) and all(n >= 0 for n in counts.values())

    assert len(rows) == counts[DocType.OTHER_DOCUMENTS] > 10
    assert len({r.doc_no for r in rows}) == len(rows)
    assert all(r.tab is DocType.OTHER_DOCUMENTS and r.title and r.date for r in rows)

    assert [f.doc_no for f in files] == [r.doc_no for r in public]
    for f in files:
        assert f.ok and f.size > 1000 and len(f.sha256) == 64
        assert f.path.read_bytes()[:5] == b"%PDF-"
    assert set(s.timings) >= {"open", "metadata", "counts", "list", "download"}


async def test_m99999_is_not_found(connector, tmp_path):
    with pytest.raises(MatterNotFound) as info:
        async with connector.session() as s:
            await s.open_matter("M99999")
    assert info.value.trace_id
    assert (tmp_path / "traces" / info.value.trace_id / "error.png").exists()


async def test_m12205_empty_tab_is_not_clicked(connector):
    async with connector.session() as s:
        await s.open_matter("M12205")
        counts = await s.read_counts()
        if counts[DocType.TRANSCRIPTS]:
            pytest.skip("M12205 has Transcripts now; the empty-tab path needs another matter")

        async def must_not_click(tab):
            raise AssertionError(f"clicked {tab}")

        s._open_tab = must_not_click
        assert await s.list_documents(DocType.TRANSCRIPTS) == []
