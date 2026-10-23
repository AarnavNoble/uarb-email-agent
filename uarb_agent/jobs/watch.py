"""WATCH: an hourly look at watched matters, emailing the watcher when a tab's count moves.

A check reads metadata and counts only (one short session). A tab is opened only when its count went up,
to name the new filings: Fetcher lists rows newest first, so they are the first `now - was` rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import structlog

from uarb_agent.models import DOC_TYPES, DocRow, DocType, MatterReport
from uarb_agent.reply.render import Reply, render_new_filing

log = structlog.get_logger(__name__)

CHECK_EVERY_S = 3600.0
MAX_WATCHES_PER_SENDER = 5

SendFn = Callable[[str, Reply], Awaitable[None]]


def snapshot(report: MatterReport) -> dict:
    """What a watch remembers: the five counts, and the newest doc_no of any tab the report listed."""
    snap: dict = {"counts": {str(t): report.counts.get(t, 0) for t in DOC_TYPES}, "newest": {}}
    if report.requested and report.rows:
        snap["newest"][str(report.requested)] = report.rows[0].doc_no
    return snap


def counts_of(snap: dict) -> dict[DocType, int]:
    return {DocType(k): int(v) for k, v in (snap.get("counts") or {}).items() if k in DocType._value2member_map_}


def changes(before: dict, report: MatterReport) -> dict[DocType, tuple[int, int]]:
    """Tabs whose count differs from the snapshot, as {tab: (was, now)}, in site order."""
    was = counts_of(before)
    return {t: (was.get(t, 0), report.counts.get(t, 0)) for t in DOC_TYPES if was.get(t, 0) != report.counts.get(t, 0)}


def alert_message(to: str, from_addr: str, reply: Reply) -> EmailMessage:
    """A new (unthreaded) email for a new-filing alert."""
    msg = EmailMessage()
    msg["From"] = formataddr(("UARB Email Agent", from_addr))
    msg["To"] = to
    msg["Subject"] = reply.subject or "New filing"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(idstring="fetcher-watch", domain=from_addr.rpartition("@")[2] or None)
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
    msg.set_content(reply.text)
    msg.add_alternative(reply.html, subtype="html")
    return msg


async def check_watches(
    ledger,
    fetcher,
    send: SendFn,
    *,
    now: float | None = None,
    min_interval_s: float = CHECK_EVERY_S,
) -> int:
    """One pass over every watch that is due. Returns how many alerts went out. Errors skip that matter only."""
    now = time.time() if now is None else now
    due: dict[str, list] = {}
    for w in ledger.list_watches():
        if w.last_checked is None or now - w.last_checked >= min_interval_s:
            due.setdefault(w.matter, []).append(w)

    sent = 0
    for matter, watches in due.items():
        try:
            fetcher.store.invalidate(matter)  # always read live counts
            report = await fetcher.fetch(matter, None)
            new_rows: dict[DocType, list[DocRow]] = {}
            for w in watches:
                diff = changes(w.snapshot, report)
                if not diff:
                    ledger.update_watch(w.sender, matter, {**w.snapshot, "counts": snapshot(report)["counts"]})
                    continue
                rows: list[DocRow] = []
                for tab, (was, now_n) in diff.items():
                    if now_n > was:
                        if tab not in new_rows:
                            new_rows[tab] = (await fetcher.fetch(matter, tab, limit=0)).rows
                        rows += new_rows[tab][: now_n - was]
                await send(w.sender, render_new_filing(report, counts_of(w.snapshot), rows))
                ledger.update_watch(w.sender, matter, snapshot(report))
                sent += 1
        except Exception as e:
            log.warning("watch.check_failed", matter=matter, error=repr(e))
    return sent


async def run_watch(ledger, fetcher, send: SendFn, stop: asyncio.Event, *, tick_s: float = 300.0) -> None:
    """Every `tick_s`, check the watches that are due (each matter at most once an hour)."""
    while not stop.is_set():
        try:
            await check_watches(ledger, fetcher, send)
        except Exception:
            log.exception("watch.tick_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), tick_s)
