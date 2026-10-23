"""uarb-agent fetch | parse | simulate | doctor | serve.

uarb-agent fetch M12205 "Other Documents" [--limit N] [--offset N]  ZIP + the reply text, straight from the site
uarb-agent parse "<email text>" [--subject S]                         what the parser makes of an email
uarb-agent simulate --from a@b --subject S --body B                   the whole pipeline as an .eml, nothing sent
uarb-agent doctor                                                     live canary on M12205; non-zero exit on failure
uarb-agent serve                                                      the email agent (inbox loop + worker + watch)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import logging
import re
import signal
import sys
import tempfile
import threading
import time
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import structlog

from uarb_agent.cache.store import Store
from uarb_agent.config import Settings, get_settings
from uarb_agent.core import Fetcher
from uarb_agent.health import Healthcheck
from uarb_agent.intent.aliases import match_type
from uarb_agent.intent.parser import parse
from uarb_agent.jobs.ledger import Ledger
from uarb_agent.jobs.worker import Pipeline, Worker
from uarb_agent.mail.inbox import parse_inbound
from uarb_agent.mail.threading import build_reply
from uarb_agent.models import DOC_TYPES, AgentError, DocType
from uarb_agent.pack.links import sweep_links
from uarb_agent.reply.render import render_report

CANARY_MATTER = "M12205"
PREVIEW_FROM = "agent@uarb-agent.invalid"


def _doc_type(value: str | None) -> DocType | None:
    if not value:
        return None
    try:
        return DocType(value)
    except ValueError:
        pass
    if t := match_type(value):
        return t
    raise SystemExit(f"unknown document type {value!r}; use one of: {', '.join(DOC_TYPES)}")


class _Stderr:
    """Whatever sys.stderr is at write time (it can be swapped after logging is configured, e.g. under pytest)."""

    def write(self, s: str) -> int:
        return sys.stderr.write(s)

    def flush(self) -> None:
        sys.stderr.flush()


def _configure_logging(verbose: bool) -> None:
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(message)s", handlers=[logging.StreamHandler(_Stderr())])
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(_Stderr()),
    )


# --- fetch ------------------------------------------------------------------------------------------------


async def _fetch(args: argparse.Namespace, settings: Settings) -> int:
    fetcher = Fetcher(settings)
    t0 = time.monotonic()
    try:
        report = await fetcher.fetch(args.matter, _doc_type(args.doc_type), args.limit, args.offset)
        out = Path(args.out) if args.out else settings.data_dir / "out" / report.trace_id
        packed = fetcher.pack(report, out)
        reply = render_report(report, batch=settings.max_docs, link_days=settings.link_ttl_days)
    except AgentError as e:
        print(f"error: {type(e).__name__}: {e} (trace {e.trace_id})", file=sys.stderr)
        return 1
    finally:
        await fetcher.close()
    print(reply.text)
    if packed and packed.path:
        print(
            f"ZIP: {packed.path} ({packed.path.stat().st_size:,} bytes, {len(packed.included)} files)", file=sys.stderr
        )
    print(f"timings: {json.dumps(report.timings)}  wall: {time.monotonic() - t0:.1f}s", file=sys.stderr)
    return 0


# --- parse ------------------------------------------------------------------------------------------------


def _parse(args: argparse.Namespace) -> int:
    result = parse(args.subject, args.text)
    print(result.model_dump_json(indent=2, exclude_defaults=True))
    return 0


# --- simulate ---------------------------------------------------------------------------------------------


def _inbound_message(args: argparse.Namespace, to: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = args.sender
    msg["To"] = to
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(idstring="sim", domain="simulate.invalid")
    if args.in_reply_to:
        msg["In-Reply-To"] = args.in_reply_to
        msg["References"] = args.in_reply_to
    msg.set_content(args.body)
    return msg


def _describe(eml: Path) -> str:
    """A short human view of a composed .eml: headers, attachments, the plain-text body."""
    import email

    msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    lines = [f"--- {eml} ({eml.stat().st_size:,} bytes)"]
    for h in ("From", "To", "Subject", "Message-ID", "In-Reply-To", "References", "Auto-Submitted", "X-Agent-Trace"):
        if msg.get(h):
            lines.append(f"{h}: {msg[h]}")
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        lines.append(f"Attachment: {part.get_filename()} ({part.get_content_type()}, {len(payload):,} bytes)")
    body = msg.get_body(preferencelist=("plain",))
    lines += ["", body.get_content() if body else "(no text body)"]
    return "\n".join(lines)


async def _simulate(args: argparse.Namespace, settings: Settings) -> int:
    agent = settings.agent_email or PREVIEW_FROM
    inbound = parse_inbound("sim", _inbound_message(args, agent).as_bytes(policy=policy.SMTP))
    ledger = Ledger(settings.data_dir / "simulate.sqlite3")  # keeps thread state so follow-ups can be simulated
    fetcher = Fetcher(settings)
    pipeline = Pipeline(fetcher, ledger, settings)
    key = inbound.thread_key
    t0 = time.monotonic()
    try:
        handled = await asyncio.wait_for(
            pipeline.handle(inbound.msg, thread_key=key, thread=ledger.get_thread(key)), 240
        )
    except Exception as e:
        handled = pipeline.failure(inbound.msg, "simulate", e)
    finally:
        await fetcher.close()
    elapsed = time.monotonic() - t0
    if handled.reply is None:
        print(f"no reply ({handled.silent_reason})")
        return 0
    if handled.thread is not None:
        ledger.save_thread(handled.thread)
    reply = build_reply(
        inbound.msg,
        from_addr=agent,
        text=handled.reply.text,
        html=handled.reply.html,
        attachments=handled.attachments,
        trace_id=handled.trace_id,
    )
    out_dir = settings.data_dir / "outbox_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", args.subject)[:40].strip("-") or "reply"
    eml = out_dir / f"{handled.trace_id}-{slug}.eml"
    eml.write_bytes(reply.as_bytes(policy=policy.SMTP))
    print(_describe(eml))
    print(f"(inbound Message-ID {inbound.message_id}; handled in {elapsed:.1f}s; nothing was sent)", file=sys.stderr)
    return 0


# --- doctor -----------------------------------------------------------------------------------------------


async def _doctor(settings: Settings) -> int:
    """Live canary: layout, metadata, counts and one real download (cache bypassed), on M12205."""
    checks: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="uarb-agent-doctor-") as tmp:
        fetcher = Fetcher(settings, store=Store(Path(tmp) / "cache"))
        t0 = time.monotonic()
        try:
            # One session: M12205's Other Documents tab is known to be non-empty and public.
            tab = DocType.OTHER_DOCUMENTS
            report = await fetcher.fetch(CANARY_MATTER, tab, limit=1, retry=False)
            md, counts = report.metadata, report.counts
            checks.append(("matter page opens and header parses", md.matter_no == CANARY_MATTER, md.title[:70]))
            checks.append(("all five tab counts parse", len(counts) == 5, json.dumps(counts)))
            checks.append(
                (
                    f"{tab} grid lists every row",
                    len(report.rows) == counts[tab] > 0,
                    f"{len(report.rows)}/{counts[tab]}",
                )
            )
            ok = report.downloaded
            pdf = bool(ok) and ok[0].path is not None and ok[0].path.read_bytes()[:4] == b"%PDF"
            detail = f"{ok[0].doc_no} {ok[0].size:,} bytes" if ok else str([f.error for f in report.files])
            checks.append(("one document downloads", bool(ok) and ok[0].size > 0, detail))
            checks.append(("it is a PDF", pdf, ""))
        except AgentError as e:
            checks.append(("site reachable and readable", False, f"{type(e).__name__}: {e} (trace {e.trace_id})"))
        finally:
            await fetcher.close()
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    passed = bool(checks) and all(ok for _, ok, _ in checks)
    print(f"doctor: {'healthy' if passed else 'UNHEALTHY'} in {time.monotonic() - t0:.1f}s")
    health = Healthcheck(settings.healthcheck_url)
    if not passed:
        health.fail("doctor failed")
    return 0 if passed else 1


# --- serve ------------------------------------------------------------------------------------------------


async def _serve(settings: Settings) -> int:
    from uarb_agent.jobs.watch import alert_message, run_watch
    from uarb_agent.jobs.worker import SettlingMailbox
    from uarb_agent.mail.inbox import ImapMailbox, run_inbox
    from uarb_agent.mail.outbox import Outbox, SmtpSslTransport

    if not (settings.agent_email and settings.agent_app_password):
        print("serve needs AGENT_EMAIL and AGENT_APP_PASSWORD (see .env.example)", file=sys.stderr)
        return 2
    user, password = settings.agent_email, settings.agent_app_password.get_secret_value()
    ledger = Ledger(settings.data_dir / "ledger.sqlite3")
    if settings.gmail_api_ready:
        from uarb_agent.mail.gmail_api import GmailApiTransport

        transport = GmailApiTransport(
            settings.gmail_client_id,
            settings.gmail_client_secret.get_secret_value(),
            settings.gmail_refresh_token.get_secret_value(),
        )
    else:
        transport = SmtpSslTransport(settings.smtp_host, settings.smtp_port, user, password)
    outbox = Outbox(ledger, transport, settings.data_dir / "outbox")
    health = Healthcheck(settings.healthcheck_url)

    def connect() -> SettlingMailbox:
        return SettlingMailbox(ImapMailbox(settings.imap_host, user, password).connect(), outbox)

    # Settle whatever a previous crash left behind before taking new work.
    first = ImapMailbox(settings.imap_host, user, password).connect()
    try:
        rec = await asyncio.to_thread(outbox.reconcile, first, reclaim_all=True)
    finally:
        first.close()
    structlog.get_logger().info("serve.reconciled", **{k: len(v) for k, v in vars(rec).items()})

    fetcher = Fetcher(settings)
    pipeline = Pipeline(fetcher, ledger, settings)
    worker = Worker(
        pipeline,
        ledger,
        outbox,
        transport,
        agent_email=user,
        exhausted=rec.exhausted,
        canary_token=settings.canary_token.get_secret_value() if settings.canary_token else None,
        # Sending itself may be what broke, so the alert goes out of band (healthchecks.io emails the owner).
        on_send_failure=lambda e: health.ping("fail", force=True),
    )
    stop, thread_stop = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def send_alert(to: str, reply) -> None:
        msg = alert_message(to, user, reply)
        await asyncio.to_thread(transport.send, msg.as_bytes(policy=policy.SMTP), user, [to])

    worker.loop = loop  # on_batch may run before worker.run gets scheduled
    worker_task = asyncio.create_task(worker.run(stop))
    watch_task = asyncio.create_task(run_watch(ledger, fetcher, send_alert, stop))
    reauth_task = asyncio.create_task(_remind_reauth(settings, transport, user, stop))
    sweep_task = asyncio.create_task(_sweep_links(settings, stop))
    inbox = asyncio.create_task(
        asyncio.to_thread(run_inbox, connect, worker.on_batch, thread_stop, on_healthy=health.ping)
    )
    stopped = asyncio.create_task(stop.wait())
    print(f"uarb-agent: serving {user}", file=sys.stderr)
    try:
        await asyncio.wait({inbox, worker_task, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if inbox.done() and inbox.exception():
            raise inbox.exception()  # an on_batch bug: exit so launchd restarts us cleanly
    finally:
        stop.set()
        thread_stop.set()
        await asyncio.gather(worker_task, watch_task, reauth_task, sweep_task, return_exceptions=True)
        await fetcher.close()
    return 0


REAUTH_DAYS = 7  # lifetime of a refresh token for an OAuth app in Google's "Testing" status


async def _remind_reauth(settings: Settings, transport, user: str, stop: asyncio.Event) -> None:
    """From a day before the Gmail API token expires, email the owner once a day to run scripts/gmail_auth.py."""
    if not (settings.gmail_api_ready and settings.gmail_token_issued and settings.owner_email):
        return
    expires = settings.gmail_token_issued + dt.timedelta(days=REAUTH_DAYS)
    reminded: dt.date | None = None
    while not stop.is_set():
        today = dt.date.today()
        if today >= expires - dt.timedelta(days=1) and reminded != today:
            msg = EmailMessage()
            msg["From"], msg["To"] = user, settings.owner_email
            msg["Subject"] = f"UARB agent: Gmail sign-in expires {expires:%b %-d}"
            msg["Auto-Submitted"] = "auto-generated"
            msg.set_content(
                f"The Gmail API token issued {settings.gmail_token_issued:%b %-d} expires on {expires:%b %-d}.\n"
                "Run `uv run python scripts/gmail_auth.py` on the Mac and copy .env to the server."
            )
            try:
                await asyncio.to_thread(transport.send, msg.as_bytes(policy=policy.SMTP), user, [settings.owner_email])
                reminded = today
            except Exception as e:
                structlog.get_logger().warning("serve.reauth_reminder_failed", error=repr(e))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), 6 * 3600)


LINK_SWEEP_S = 3600


async def _sweep_links(settings: Settings, stop: asyncio.Event) -> None:
    """Delete expired download links at startup and then hourly, while links are being published."""
    if not settings.public_base_url:
        return
    while not stop.is_set():
        try:
            now = dt.datetime.now(dt.UTC)
            await asyncio.to_thread(sweep_links, settings.links_dir, settings.link_ttl_days, now)
        except Exception as e:
            structlog.get_logger().warning("serve.link_sweep_failed", error=repr(e))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), LINK_SWEEP_S)


def _skip_backlog(settings: Settings) -> int:
    """Mark everything already in the inbox as handled, so a first `serve` never answers old mail."""
    from uarb_agent.mail.inbox import ImapMailbox

    if not (settings.agent_email and settings.agent_app_password):
        print("skip-backlog needs AGENT_EMAIL and AGENT_APP_PASSWORD (see .env.example)", file=sys.stderr)
        return 2
    box = ImapMailbox(settings.imap_host, settings.agent_email, settings.agent_app_password.get_secret_value())
    box.connect()
    try:
        backlog = box.fetch_unseen()
        for ib in backlog:
            box.mark_done(ib.uid)
            print(f"skipped  {ib.sender:<40} {ib.subject[:60]}")
    finally:
        box.close()
    print(f"{len(backlog)} message(s) marked handled")
    return 0


# --- main -------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uarb-agent", description="UARB email agent")
    p.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="fetch a matter's documents, write the ZIP and print the reply text")
    f.add_argument("matter")
    f.add_argument("doc_type", nargs="?", help="one of: " + ", ".join(DOC_TYPES) + " (omit for a summary)")
    f.add_argument("--limit", type=int, default=None)
    f.add_argument("--offset", type=int, default=0)
    f.add_argument("--out", help="directory for the ZIP (default data/out/<trace id>)")

    pa = sub.add_parser("parse", help="show how an email would be parsed")
    pa.add_argument("text")
    pa.add_argument("--subject", default="")

    s = sub.add_parser("simulate", help="run the full pipeline on a made-up email; writes an .eml, sends nothing")
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--subject", default="")
    s.add_argument("--body", default="")
    s.add_argument("--in-reply-to", help="Message-ID of an earlier simulated email, to test follow-ups")

    sub.add_parser("doctor", help="live canary against M12205; exits non-zero on failure")
    sub.add_parser("serve", help="run the email agent")
    sub.add_parser("skip-backlog", help="mark mail already in the inbox as handled (run once before the first serve)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    _configure_logging(args.verbose or args.cmd == "serve")
    settings = get_settings()
    if args.cmd == "parse":
        return _parse(args)
    if args.cmd == "fetch":
        args.limit = settings.max_docs if args.limit is None else max(0, min(args.limit, settings.max_docs))
        return asyncio.run(_fetch(args, settings))
    if args.cmd == "simulate":
        return asyncio.run(_simulate(args, settings))
    if args.cmd == "doctor":
        return asyncio.run(_doctor(settings))
    if args.cmd == "serve":
        return asyncio.run(_serve(settings))
    if args.cmd == "skip-backlog":
        return _skip_backlog(settings)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
