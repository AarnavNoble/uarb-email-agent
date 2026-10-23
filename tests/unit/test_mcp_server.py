import sys
import types
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fake_site import COUNTS, FakeSite, make_rows
from uarb_agent.cache.store import Store
from uarb_agent.config import Settings
from uarb_agent.core import Fetcher
from uarb_agent.mcp_server import Tools, build_server, parse_doc_type
from uarb_agent.models import DocType

OTHER = DocType.OTHER_DOCUMENTS


def on_disk(path: str) -> bool:
    return Path(path).is_absolute() and Path(path).exists()


def tools_for(tmp_path, site: FakeSite, **settings) -> Tools:
    s = Settings(data_dir=tmp_path / "data", **settings)
    return Tools(s, fetcher_factory=lambda: Fetcher(s, connector=site, store=Store(s.cache_dir)))


async def test_get_matter_gives_metadata_counts_and_total(tmp_path):
    info = await tools_for(tmp_path, FakeSite()).get_matter("m-12205")
    assert info.metadata.matter_no == "M12205"
    assert list(info.counts) == [str(t) for t in DocType]
    assert info.counts["Transcripts"] == 0
    assert info.total == sum(COUNTS.values())


async def test_errors_become_tool_errors(tmp_path):
    tools = tools_for(tmp_path, FakeSite())
    with pytest.raises(ToolError, match="InvalidMatterNumber"):
        await tools.get_matter("12205")
    with pytest.raises(ToolError, match="MatterNotFound"):
        await tools.get_matter("M99999")
    with pytest.raises(ToolError, match="use one of"):
        await tools.list_documents("M12205", "spreadsheets")


def test_doc_type_accepts_aliases():
    assert parse_doc_type("Key Documents") is DocType.KEY_DOCUMENTS
    assert parse_doc_type("other docs") is OTHER


async def test_list_documents_pages_newest_first_without_downloading(tmp_path):
    rows = {t: make_rows(t, n) for t, n in COUNTS.items()}
    rows[OTHER] = make_rows(OTHER, 14, confidential=frozenset({1}))
    site = FakeSite(rows=rows)
    listing = await tools_for(tmp_path, site).list_documents("M12205", "other documents", limit=5, offset=0)
    assert listing.total == 14 and listing.doc_type == "Other Documents"
    assert [d.doc_no for d in listing.documents] == [r.doc_no for r in rows[OTHER][:5]]
    assert [d.public for d in listing.documents[:2]] == [True, False]
    assert listing.documents[0].date == "2026-06-01"
    assert site.downloaded == []

    page = await tools_for(tmp_path, site).list_documents("M12205", "Other Documents", limit=5, offset=10)
    assert [d.doc_no for d in page.documents] == [r.doc_no for r in rows[OTHER][10:14]]


async def test_download_returns_local_paths_zip_and_next_offset(tmp_path):
    rows = {t: make_rows(t, n) for t, n in COUNTS.items()}
    rows[OTHER] = make_rows(OTHER, 14, confidential=frozenset({0}))
    site = FakeSite(rows=rows)
    result = await tools_for(tmp_path, site).download_documents("M12205", "Other Documents", limit=3)
    assert [f.doc_no for f in result.files] == [r.doc_no for r in rows[OTHER][1:4]]
    assert all(f.path and on_disk(f.path) for f in result.files)
    assert [s.doc_no for s in result.skipped] == [rows[OTHER][0].doc_no]
    assert result.next_offset == 4 and result.available == 14
    assert result.zip_path and zipfile.is_zipfile(result.zip_path)


async def test_download_is_capped_at_max_docs_and_ends_with_no_next_offset(tmp_path):
    site = FakeSite()
    tools = tools_for(tmp_path, site)
    first = await tools.download_documents("M12205", "Other Documents", limit=50)
    assert len(first.files) == 10 and first.next_offset == 10
    rest = await tools.download_documents("M12205", "Other Documents", limit=50, offset=first.next_offset)
    assert len(rest.files) == 4 and rest.next_offset is None


async def test_download_of_an_empty_tab_has_no_zip(tmp_path):
    result = await tools_for(tmp_path, FakeSite()).download_documents("M12205", "Transcripts")
    assert result.files == [] and result.zip_path is None and result.next_offset is None


async def test_oversized_file_keeps_its_local_path_and_gets_a_link(tmp_path):
    site = FakeSite(file_size=20_000)  # ~120 KB each
    tools = tools_for(tmp_path, site, zip_budget_mb=0, public_base_url="https://example.invalid")
    result = await tools.download_documents("M12205", "Key Documents", limit=1)
    (f,) = result.files
    assert f.path and on_disk(f.path)
    assert f.url and f.url.startswith("https://example.invalid/d/")


async def test_search_without_the_module_is_a_clear_tool_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "uarb_agent.search", None)  # makes the import raise ImportError
    with pytest.raises(ToolError, match="not available"):
        await tools_for(tmp_path, FakeSite()).search("semi-annual")


async def test_search_passes_through_sync_and_async_backends(tmp_path, monkeypatch):
    calls = []

    def sync_search(query, matter=None, limit=5):
        calls.append((query, matter, limit))
        return [{"matter": "M12205", "doc_no": "102674", "page": 1, "snippet": "total project cost"}]

    async def async_search(query, matter=None, limit=5):
        return sync_search(query, matter, limit)

    tools = tools_for(tmp_path, FakeSite())
    for backend in (sync_search, async_search):
        monkeypatch.setitem(sys.modules, "uarb_agent.search", types.SimpleNamespace(search=backend))
        hits = await tools.search("project cost", matter="M12205", limit=3)
        assert hits[0]["doc_no"] == "102674"
    assert calls == [("project cost", "M12205", 3)] * 2


async def test_search_falls_back_to_open_index(tmp_path, monkeypatch):
    @dataclass(frozen=True)
    class Hit:
        doc_no: str
        page: int

    opened = []

    class Index:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            opened.append("closed")

        def search(self, query, matter=None, limit=5):
            return [Hit("102674", 1)][:limit]

    def open_index(settings):
        opened.append(settings.data_dir)
        return Index()

    monkeypatch.setitem(sys.modules, "uarb_agent.search", types.SimpleNamespace(open_index=open_index))
    tools = tools_for(tmp_path, FakeSite())
    assert await tools.search("cost") == [{"doc_no": "102674", "page": 1}]
    assert opened == [tmp_path / "data", "closed"]


async def test_server_registers_read_only_tools_and_calls_through(tmp_path):
    site = FakeSite()
    tools = tools_for(tmp_path, site)
    server = build_server(tools)
    listed = {t.name: t for t in await server.list_tools()}
    assert set(listed) == {"get_matter", "list_documents", "download_documents", "search"}
    assert all(t.annotations and t.annotations.read_only_hint for t in listed.values())
    assert listed["get_matter"].output_schema is not None

    result = await server.call_tool("get_matter", {"matter": "M12205"})
    assert not result.is_error
    assert result.structured_content["total"] == sum(COUNTS.values())

    with pytest.raises(ToolError, match="MatterNotFound"):
        await server.call_tool("get_matter", {"matter": "M99999"})
    await tools.close()
    assert site.closed


def test_listing_tools_never_starts_a_fetcher(tmp_path):
    made = []
    tools = Tools(Settings(data_dir=tmp_path), fetcher_factory=lambda: made.append(1))  # type: ignore[arg-type,return-value]
    build_server(tools)
    assert made == []
