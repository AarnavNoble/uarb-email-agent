"""Expiring download links for files too large to email.

Each published file lives at <links_dir>/<token>/<filename>; the web server serves links_dir at /d/, so the
unguessable token is the only access control. `sweep_links` removes token directories once they pass their TTL.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import shutil
import urllib.parse
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


def publish_link(src: Path, filename: str, *, links_dir: Path, base_url: str) -> str:
    """Hardlink (or copy, across filesystems) `src` to links_dir/<token>/<filename> and return its public URL."""
    token = secrets.token_urlsafe(16)
    dest = links_dir / token / filename
    dest.parent.mkdir(parents=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)
    # The cache writes 0600 temp files; the web server runs as another user. These are public UARB documents.
    os.chmod(dest, 0o644)
    return f"{base_url.rstrip('/')}/d/{token}/{urllib.parse.quote(filename)}"


def sweep_links(links_dir: Path, ttl_days: int, now: dt.datetime) -> int:
    """Delete token directories last modified more than `ttl_days` before `now`; returns how many went."""
    if not links_dir.is_dir():
        return 0
    cutoff = (now - dt.timedelta(days=ttl_days)).timestamp()
    removed = 0
    for d in links_dir.iterdir():
        if d.is_dir() and not d.is_symlink() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        log.info("links.swept", removed=removed)
    return removed
