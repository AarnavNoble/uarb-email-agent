"""From an inbound email to a composed reply, and the queue that runs that for the inbox.

`Pipeline.handle` is the whole agent minus the mail transport: parse -> fetch -> zip -> render. It returns a
`Handled` that always carries a reply unless the email needs none (a "thanks", an auto-reply). The CLI's
`simulate` command runs it directly; `Worker` runs it for the IMAP loop with hard timeouts and exactly-once
delivery through the ledger and outbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage, Message
from pathlib import Path

import structlog

from uarb_agent.config import Settings
from uarb_agent.core import Fetcher, as_error, new_trace_id
from uarb_agent.intent.aliases import match_type
from uarb_agent.intent.parser import batch_note, clean_body, parse
from uarb_agent.jobs.ledger import TERMINAL, Ledger, Status, ThreadState
from uarb_agent.jobs.watch import MAX_WATCHES_PER_SENDER, snapshot
from uarb_agent.mail.guards import screen
from uarb_agent.mail.inbox import Inbound, MailboxClient
from uarb_agent.mail.outbox import MAX_ATTEMPTS, Outbox, SmtpTransport
from uarb_agent.mail.threading import build_reply, sender_address
from uarb_agent.models import (
    AgentError,
    Clarify,
    CommandKind,
    DocType,
    MatterReport,
    ParseResult,
    Request,
    SiteUnavailable,
)
from uarb_agent.pack.zipper import MB
from uarb_agent.reply.render import (
    Outcome,
    Reply,
    ReplyCheckFailed,
    render_ack,
    render_clarify,
    render_help,
    render_outcomes,
    render_unwatch,
    render_watch_confirm,
)

log = structlog.get_logger(__name__)

REQUEST_TIMEOUT_S = 120.0
EMAIL_TIMEOUT_S = 240.0
ACK_AFTER_S = 75.0
LEASE_S = 300.0
_HELP = re.compile(r"(?i)^\W*(help|commands|what can you do)\W*$")
SILENT = frozenset({"auto_reply", "no_request"})  # clarify reasons that get no reply at all

AckFn = Callable[[Sequence[Request]], Awaitable[None]]


@dataclass
class Handled:
    reply: Reply | None  # None: this email needs no answer (see `silent_reason`)
    trace_id: str
    attachments: list[Path] = field(default_factory=list)
    thread: ThreadState | None = None  # state to save for follow-ups in this thread
    silent_reason: str | None = None


def bodies(msg: Message) -> tuple[str, str | None]:
    """(plain text, html) of an email; either may be empty when the sender's client omitted it."""
    if not isinstance(msg, EmailMessage):
        return str(msg.get_payload() or ""), None

    def part(kind: str) -> str | None:
        p = msg.get_body(preferencelist=(kind,))
        if p is None:
            return None
        try:
            return p.get_content()
        except (LookupError, UnicodeError):  # unknown charset
            return p.get_payload(decode=True).decode("utf-8", "replace")

    return part("plain") or "", part("html")


def next_offset(report: MatterReport) -> int:
    """Where MORE resumes: one past the furthest row this report attempted or passed over."""
    position = {r.doc_no: i for i, r in enumerate(report.rows)}
    touched = [
        position[d] for d in [f.doc_no for f in report.files] + [s.doc_no for s in report.skipped] if d in position
    ]
    return max(touched) + 1 if touched else report.offset


def _has_more(report: MatterReport) -> bool:
    return report.requested is not None and report.counts.get(report.requested, 0) > next_offset(report)


class Pipeline:
    def __init__(self, fetcher: Fetcher, ledger: Ledger, settings: Settings, *, out_dir: Path | None = None):
        self.fetcher = fetcher
        self.ledger = ledger
        self.settings = settings
        self.out_dir = out_dir or settings.data_dir / "out"
        self.request_timeout_s = REQUEST_TIMEOUT_S
        self.ack_after_s = ACK_AFTER_S

    async def handle(
        self,
        msg: Message,
        *,
        thread_key: str,
        thread: ThreadState | None = None,
        trace_id: str | None = None,
        ack: AckFn | None = None,
    ) -> Handled:
        trace_id = trace_id or new_trace_id()
        sender = sender_address(msg)
        text, html = bodies(msg)
        subject = str(msg.get("Subject", ""))
        # In a thread: the optional LLM fallback is a blocking HTTP call (~30 s when the API is unreachable) and
        # must not freeze the other consumer's browser session or the ack timer.
        pr = await asyncio.to_thread(parse, subject, text, html, thread, max_docs=self.settings.max_docs)
        state = thread.model_copy(deep=True) if thread else ThreadState(thread_key=thread_key)
        log.info(
            "pipeline.parsed",
            trace_id=trace_id,
            requests=[r.model_dump(mode="json") for r in pr.requests],
            clarify=pr.clarify and pr.clarify.reason,
            command=pr.command and pr.command.kind,
        )

        if pr.clarify and pr.clarify.reason in SILENT:
            return Handled(None, trace_id, silent_reason=pr.clarify.reason)
        if pr.command and pr.command.kind is not CommandKind.MORE and not pr.clarify:
            return await self._command(pr, sender, state, trace_id)
        if pr.clarify and pr.clarify.reason == "no_matter" and _HELP.match(clean_body(text, html) or subject):
            return Handled(render_help(batch=self.settings.max_docs), trace_id)
        if pr.clarify:
            state.pending = self._pending_for(pr.clarify, f"{subject}\n{text}", state)
            return Handled(render_clarify(pr.clarify, batch=self.settings.max_docs), trace_id, thread=state)
        return await self._requests(pr, state, trace_id, ack)

    # --- requests -------------------------------------------------------------------------------

    async def _requests(self, pr: ParseResult, state: ThreadState, trace_id: str, ack: AckFn | None) -> Handled:
        acker = asyncio.create_task(self._ack_later(ack, pr.requests)) if ack else None
        try:
            outcomes = await asyncio.gather(
                *(
                    self._run(r, f"{trace_id}-{i}" if len(pr.requests) > 1 else trace_id)
                    for i, r in enumerate(pr.requests)
                )
            )
        finally:
            if acker:
                acker.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await acker

        attachments = self._pack(outcomes, trace_id)
        notes = pr.notes
        if not any(_has_more(o.report) for o in outcomes if o.report):
            notes = [n for n in notes if n != batch_note(self.settings.max_docs)]
        try:
            reply = render_outcomes(outcomes, notes=notes, batch=self.settings.max_docs)
        except ReplyCheckFailed as e:
            # Never send a number we cannot source. Fall back to an honest failure without attachments.
            log.error("pipeline.reply_check_failed", trace_id=trace_id, numbers=sorted(e.numbers))
            err = SiteUnavailable("reply self-check failed", trace_id=trace_id)
            reply = render_outcomes([Outcome(o.request, error=err) for o in outcomes], notes=notes)
            attachments = []

        self._remember(state, outcomes)
        return Handled(reply, trace_id, attachments=attachments, thread=state)

    async def _run(self, req: Request, trace_id: str) -> Outcome:
        try:
            report = await asyncio.wait_for(
                self.fetcher.fetch(req.matter, req.doc_type, req.limit, req.offset, trace_id=trace_id),
                self.request_timeout_s,
            )
            return Outcome(req, report=report)
        except Exception as e:
            err = as_error(e, trace_id)
            log.warning("pipeline.request_failed", trace_id=trace_id, matter=req.matter, error=repr(err))
            return Outcome(req, error=err)

    def _pack(self, outcomes: Sequence[Outcome], trace_id: str) -> list[Path]:
        budget = self.settings.zip_budget_mb * MB
        out = []
        for o in outcomes:
            if o.report is None:
                continue
            packed = self.fetcher.pack(o.report, self.out_dir / trace_id, budget_bytes=budget)
            if packed and packed.path:
                out.append(packed.path)
                budget -= packed.path.stat().st_size
        return out

    async def _ack_later(self, ack: AckFn, requests: Sequence[Request]) -> None:
        await asyncio.sleep(self.ack_after_s)
        await ack(requests)

    def _remember(self, state: ThreadState, outcomes: Sequence[Outcome]) -> None:
        state.pending = None
        reports = [o.report for o in outcomes if o.report]
        if len(outcomes) != 1 or not reports:
            return
        report = reports[0]
        state.matter = report.metadata.matter_no
        state.doc_type = str(report.requested) if report.requested else None
        if report.requested is None:
            state.pending = {"slot": "doc_type", "matter": state.matter}
            state.next_offset = 0
        else:
            state.next_offset = next_offset(report)
            state.sent_doc_nos += [f.doc_no for f in report.downloaded if f.doc_no not in state.sent_doc_nos]

    @staticmethod
    def _pending_for(clarify: Clarify, text: str, state: ThreadState) -> dict | None:
        if clarify.reason in ("no_matter", "invalid_matter_like"):
            t = match_type(text)
            return {"slot": "matter", "doc_type": str(t) if t else None}
        if clarify.reason == "unknown_type":
            return state.pending
        return None

    # --- commands -------------------------------------------------------------------------------

    async def _command(self, pr: ParseResult, sender: str, state: ThreadState, trace_id: str) -> Handled:
        assert pr.command is not None
        kind, args = pr.command.kind, pr.command.args
        matter = args[-1]
        try:
            if kind is CommandKind.UNWATCH:
                return Handled(render_unwatch(matter, was_watching=self.ledger.remove_watch(sender, matter)), trace_id)

            if kind is CommandKind.WATCH:
                watching = {w.matter for w in self.ledger.list_watches(sender)}
                if matter not in watching and len(watching) >= MAX_WATCHES_PER_SENDER:
                    q = (
                        f"You're already watching {len(watching)} matters, which is the limit. "
                        "Reply UNWATCH and a matter number to free a slot."
                    )
                    return Handled(render_clarify(Clarify(reason="watch_limit", question=q)), trace_id)
                report = await asyncio.wait_for(
                    self.fetcher.fetch(matter, None, trace_id=trace_id), self.request_timeout_s
                )
                self.ledger.add_watch(sender, matter, snapshot(report))
                state.matter = matter
                return Handled(render_watch_confirm(report), trace_id, thread=state)

            # GET <doc_no>
            doc_no = args[0]
            prefer = DocType(state.doc_type) if state.doc_type else None
            report = await asyncio.wait_for(
                self.fetcher.find_doc(matter, doc_no, prefer, trace_id=trace_id), self.request_timeout_s
            )
            if report is None:
                q = (
                    f"I couldn't find document {doc_no} in {matter}. Document numbers are in the list I sent, "
                    "or on the matter's page at uarb.novascotia.ca."
                )
                return Handled(render_clarify(Clarify(reason="doc_not_found", question=q)), trace_id, thread=state)
            req = Request(matter=matter, doc_type=report.requested, limit=1, offset=report.offset)
            outcome = Outcome(req, report=report)
            attachments = self._pack([outcome], trace_id)
            reply = render_outcomes([outcome])
            self._remember(state, [outcome])  # the reply offers MORE from after this document, so honour it
            return Handled(reply, trace_id, attachments=attachments, thread=state)
        except Exception as e:
            err = as_error(e, trace_id)
            req = Request(matter=matter)
            return Handled(render_outcomes([Outcome(req, error=err)]), trace_id, thread=state)

    # --- failure --------------------------------------------------------------------------------

    def failure(self, msg: Message, trace_id: str, exc: BaseException | None = None) -> Handled:
        """The reply of last resort: honest, no invented facts, with a reference the owner can trace."""
        if exc is not None:
            log.error("pipeline.failed", trace_id=trace_id, error=repr(exc))
        try:
            text, html = bodies(msg)
            requests = parse(str(msg.get("Subject", "")), text, html, llm=None).requests
        except Exception:
            requests = []
        err: AgentError = SiteUnavailable("job did not finish", trace_id=trace_id)
        if requests:
            return Handled(render_outcomes([Outcome(r, error=err) for r in requests]), trace_id)
        body = (
            "Sorry, something went wrong on my side while handling your email, so I stopped rather than guess. "
            f"Please try again in a little while. If it keeps happening, mention this reference: {trace_id}.\n"
        )
        return Handled(Reply(text=body, html=f"<p>{body.strip()}</p>\n"), trace_id)


# --- the worker -------------------------------------------------------------------------------------------


@dataclass
class Task:
    inbound: Inbound
    apology: bool = False  # the job ran out of attempts in earlier runs; just say so


class SettlingMailbox:
    """Wraps the IMAP client so finished jobs are labelled done on the IMAP thread before every fetch."""

    def __init__(self, inner: MailboxClient, outbox: Outbox):
        self.inner, self.outbox = inner, outbox

    def fetch_unseen(self) -> list[Inbound]:
        self.outbox.settle(self.inner)
        return self.inner.fetch_unseen()

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class Worker:
    """Bounded queue, N consumers. The IMAP thread feeds it through `on_batch`; consumers reply via the outbox."""

    def __init__(
        self,
        pipeline: Pipeline,
        ledger: Ledger,
        outbox: Outbox,
        transport: SmtpTransport,
        *,
        agent_email: str,
        consumers: int = 2,
        queue_size: int = 50,
        email_timeout_s: float = EMAIL_TIMEOUT_S,
        exhausted: Sequence[str] = (),
        canary_token: str | None = None,
        on_send_failure: Callable[[BaseException], None] | None = None,
    ):
        self.pipeline, self.ledger, self.outbox, self.transport = pipeline, ledger, outbox, transport
        self.agent_email = agent_email
        self.canary_token = canary_token
        self.on_send_failure = on_send_failure
        self.consumers = consumers
        self.email_timeout_s = email_timeout_s
        self.queue: asyncio.Queue[Task] = asyncio.Queue(maxsize=queue_size)
        self.exhausted = set(exhausted)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._active: set[str] = set()  # claimed by this process and not yet through `process`

    # --- IMAP thread ----------------------------------------------------------------------------

    def on_batch(self, client: MailboxClient, batch: list[Inbound]) -> None:
        """Called on the IMAP thread. Screens, records and claims each message, then hands it to the queue
        (blocking when the queue is full, which is the backpressure we want)."""
        assert self.loop is not None, "start run() first"
        for ib in batch:
            for task in self._admit(client, ib):
                asyncio.run_coroutine_threadsafe(self.queue.put(task), self.loop).result()

    def _admit(self, client: MailboxClient, ib: Inbound) -> list[Task]:
        with self._lock:
            mid = ib.message_id
            job = self.ledger.get(mid)
            if job is None:
                reason = screen(
                    ib.msg, self.ledger, [self.agent_email], thread_key=ib.thread_key, canary_token=self.canary_token
                )
                self.ledger.record(mid, uid=ib.uid, sender=ib.sender, subject=ib.subject, thread_key=ib.thread_key)
                if reason:
                    log.info("worker.ignored", message_id=mid, reason=reason)
                    self.ledger.transition(mid, Status.RECEIVED, Status.IGNORED, reason=reason)
                    client.mark_done(ib.uid)
                    return []
                job = self.ledger.get(mid)
            assert job is not None
            if job.status == Status.RECEIVED and self.ledger.claim(mid, lease_s=LEASE_S):
                self._active.add(mid)
                # A job that keeps crashing the worker gets the honest failure reply instead of another run.
                return [Task(ib, apology=job.attempts >= MAX_ATTEMPTS)]
            if job.status == Status.WORKING and mid in self.exhausted and self.ledger.renew(mid, lease_s=LEASE_S):
                self.exhausted.discard(mid)
                self._active.add(mid)
                return [Task(ib, apology=True)]
            if job.status == Status.COMPOSED and mid not in self._active:
                # The consumer ran out of SMTP retries. The original is still unseen, so it comes back every IMAP
                # round; finish the send here rather than leave it until the next restart's reconcile.
                try:
                    self.outbox.retry_composed(client, mid)
                except Exception as e:
                    log.warning("worker.resend_failed", message_id=mid, error=repr(e))
                return []
            if job.status in TERMINAL:
                client.mark_done(ib.uid)  # a duplicate delivery of something already answered
            return []

    # --- event loop -----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        self.loop = asyncio.get_running_loop()
        consumers = [asyncio.create_task(self._consume(), name=f"consumer-{i}") for i in range(self.consumers)]
        try:
            await stop.wait()
        finally:
            for c in consumers:
                c.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)

    async def _consume(self) -> None:
        while True:
            task = await self.queue.get()
            mid = task.inbound.message_id
            try:
                await self.process(task)
            except Exception:
                log.exception("worker.process_crashed", message_id=mid)
                self._give_back(mid)
            finally:
                with self._lock:
                    self._active.discard(mid)
                self.queue.task_done()

    def _give_back(self, mid: str) -> None:
        """After a crash, return a still-working job to `received` so the next IMAP round picks it up again
        (as an apology once it is out of attempts), instead of leaving it stuck until a restart."""
        job = self.ledger.get(mid)
        if job is None or job.status != Status.WORKING:
            return
        to = Status.RECEIVED if job.attempts <= MAX_ATTEMPTS else Status.FAILED
        self.ledger.transition(mid, Status.WORKING, to, reason="worker crashed")

    async def process(self, task: Task) -> None:
        ib, mid = task.inbound, task.inbound.message_id
        trace_id = new_trace_id()
        thread = self.ledger.get_thread(ib.thread_key)
        if task.apology:
            handled = self.pipeline.failure(ib.msg, trace_id)
        else:
            try:
                handled = await asyncio.wait_for(
                    self.pipeline.handle(
                        ib.msg, thread_key=ib.thread_key, thread=thread, trace_id=trace_id, ack=self._acker(ib)
                    ),
                    self.email_timeout_s,
                )
            except Exception as e:  # includes the per-email TimeoutError
                handled = self.pipeline.failure(ib.msg, trace_id, e)

        if handled.reply is None:
            self.ledger.transition(
                mid, Status.WORKING, Status.FAILED, reason=f"no reply needed: {handled.silent_reason}"
            )
            return
        reply = build_reply(
            ib.msg,
            from_addr=self.agent_email,
            text=handled.reply.text,
            html=handled.reply.html,
            attachments=handled.attachments,
            trace_id=handled.trace_id,
        )
        await asyncio.to_thread(self.outbox.compose, mid, reply)
        # The .eml now holds the attachment bytes; the working ZIPs are no longer needed.
        await asyncio.to_thread(shutil.rmtree, self.pipeline.out_dir / handled.trace_id, ignore_errors=True)
        if handled.thread is not None:
            self.ledger.save_thread(handled.thread)
        log.info("worker.composed", message_id=mid, trace_id=handled.trace_id, attachments=len(handled.attachments))
        await self._send(mid)

    async def _send(self, mid: str) -> None:
        """One attempt. A failed send may still have been accepted (a reset on QUIT, a lost final 250), so it is
        not blindly repeated here: the row stays composed and the next IMAP round retries it via `_admit`,
        which looks in Sent Mail first."""
        try:
            await asyncio.to_thread(self.outbox.send, mid)
        except Exception as e:
            log.warning("worker.send_failed", message_id=mid, error=repr(e))
            if self.on_send_failure:
                self.on_send_failure(e)
            return
        log.info("worker.sent", message_id=mid)

    def _acker(self, ib: Inbound) -> AckFn:
        async def ack(requests: Sequence[Request]) -> None:
            reply = render_ack(requests)
            msg = build_reply(ib.msg, from_addr=self.agent_email, text=reply.text, html=reply.html)
            raw = msg.as_bytes(policy=policy.SMTP)
            await asyncio.to_thread(self.transport.send, raw, self.agent_email, [str(msg["To"])])
            log.info("worker.ack_sent", message_id=ib.message_id)

        return ack
