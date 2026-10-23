import asyncio
import datetime as dt
import os

from uarb_agent.cli import _sweep_links
from uarb_agent.config import Settings
from uarb_agent.pack.links import publish_link, sweep_links

NOW = dt.datetime(2026, 10, 23, 14, 30, tzinfo=dt.UTC)
BASE = "https://201-79-51-198.sslip.io"


def test_publish_places_file_under_token_dir(tmp_path):
    src = tmp_path / "cache" / "a.pdf"
    src.parent.mkdir()
    src.write_bytes(b"%PDF big")
    links = tmp_path / "links"
    url = publish_link(src, "E-3 - Evidence.pdf", links_dir=links, base_url=BASE)

    [token_dir] = links.iterdir()
    assert (token_dir / "E-3 - Evidence.pdf").read_bytes() == b"%PDF big"
    assert len(token_dir.name) >= 20  # token_urlsafe(16) is 22 characters
    assert url == f"{BASE}/d/{token_dir.name}/E-3%20-%20Evidence.pdf"
    assert src.exists()  # the cache keeps its copy


def test_publish_quotes_parentheses_and_strips_trailing_slash(tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"x")
    url = publish_link(src, "H-4(C)-i - NSPI Evidence (Part 2).pdf", links_dir=tmp_path / "links", base_url=BASE + "/")
    assert url.startswith(f"{BASE}/d/")
    assert url.endswith("/H-4%28C%29-i%20-%20NSPI%20Evidence%20%28Part%202%29.pdf")


def test_each_publish_gets_a_fresh_token(tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"x")
    urls = {publish_link(src, "a.pdf", links_dir=tmp_path / "links", base_url=BASE) for _ in range(3)}
    assert len(urls) == 3


def test_sweep_deletes_expired_and_keeps_fresh(tmp_path):
    links = tmp_path / "links"
    old, fresh = links / "old", links / "fresh"
    for d in (old, fresh):
        d.mkdir(parents=True)
        (d / "f.pdf").write_bytes(b"x")
    eight_days_ago = (NOW - dt.timedelta(days=8)).timestamp()
    os.utime(old, (eight_days_ago, eight_days_ago))
    an_hour_ago = (NOW - dt.timedelta(hours=1)).timestamp()
    os.utime(fresh, (an_hour_ago, an_hour_ago))

    assert sweep_links(links, 7, NOW) == 1
    assert not old.exists()
    assert (fresh / "f.pdf").exists()


def test_sweep_without_links_dir(tmp_path):
    assert sweep_links(tmp_path / "nope", 7, NOW) == 0


async def test_serve_sweeps_at_startup_and_stops(tmp_path):
    s = Settings(data_dir=tmp_path, public_base_url=BASE)
    old = s.links_dir / "old"
    old.mkdir(parents=True)
    long_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(days=30)).timestamp()
    os.utime(old, (long_ago, long_ago))
    stop = asyncio.Event()
    task = asyncio.create_task(_sweep_links(s, stop))
    for _ in range(100):
        if not old.exists():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert not old.exists()


def test_published_files_are_world_readable_for_the_web_server(tmp_path):
    import stat

    from uarb_agent.pack.links import publish_link

    src = tmp_path / "blob"
    src.write_bytes(b"%PDF")
    src.chmod(0o600)
    url = publish_link(src, "a.pdf", links_dir=tmp_path / "links", base_url="https://x")
    dest = tmp_path / "links" / url.split("/d/")[1].split("/")[0] / "a.pdf"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644
