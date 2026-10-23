"""Sending side, built so that a crash at any point yields exactly one reply per inbound message.

1. compose: write the reply to disk as an .eml and store its Message-ID in the ledger (working -> composed).
2. send:    SMTP the exact bytes of that .eml (composed -> sent).
3. settle:  on the IMAP thread, label the original 'uarb-agent/done' and mark it \\Seen (sent -> done).

After a crash, `reconcile` looks up each composed/sent row's reply Message-ID in Sent Mail. Found means SMTP
accepted it before we died; not found means it never left, so the saved .eml is resent byte for byte, with the
same Message-ID, which keeps the thread intact and makes a later reconcile find it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import smtplib
from collections.abc import Sequence
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Protocol

from uarb_agent.jobs.ledger import Ledger, Status

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class SmtpTransport(Protocol):
    def send(self, raw: bytes, from_addr: str, to_addrs: Sequence[str]) -> None: ...


class SentLookup(Protocol):
    """The two IMAP operations the outbox needs (MailboxClient satisfies it)."""

    def sent_contains(self, message_id: str) -> bool: ...

    def mark_done(self, uid: str) -> None: ...


class SmtpSslTransport:
    def __init__(self, host: str, port: int, user: str, password: str, *, timeout: float = 60):
        self.host, self.port, self.user, self._password, self.timeout = host, port, user, password, timeout

    def send(self, raw: bytes, from_addr: str, to_addrs: Sequence[str]) -> None:
        smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        try:
            smtp.login(self.user, self._password)
            smtp.sendmail(from_addr, list(to_addrs), raw)
        finally:
            # Once sendmail returns the message is accepted; a failed QUIT must not make it look unsent.
            try:
                smtp.quit()
            except Exception:
                smtp.close()


def is_permanent(exc: BaseException) -> bool:
    """5xx answers and refused recipients will fail the same way next time; everything else is worth a retry."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused | smtplib.SMTPSenderRefused):
        return True
    if getattr(exc, "permanent", False):  # GmailApiError: a 4xx will fail the same way next time
        return True
    return isinstance(exc, smtplib.SMTPResponseException) and 500 <= exc.smtp_code < 600


class StaleJob(RuntimeError):
    """The ledger row was not in the state this step expects (lost lease, or another process got there first)."""


@dataclass
class Reconciled:
    confirmed: list[str] = field(default_factory=list)  # reply was already in Sent Mail
    resent: list[str] = field(default_factory=list)  # reply never left; resent from the saved .eml
    requeued: list[str] = field(default_factory=list)  # lease expired mid-job; back to received for another try
    exhausted: list[str] = field(default_factory=list)  # out of attempts; caller owes the sender an apology


def _envelope(raw: bytes) -> tuple[str, list[str]]:
    headers = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(headers.get("From", "")))[1]
    rcpts = [a for _, a in getaddresses([str(v) for h in ("To", "Cc") for v in headers.get_all(h, [])]) if a]
    return sender, rcpts


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class Outbox:
    def __init__(self, ledger: Ledger, transport: SmtpTransport, outbox_dir: Path):
        self.ledger = ledger
        self.transport = transport
        self.dir = Path(outbox_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def eml_path(self, message_id: str) -> Path:
        return self.dir / f"{hashlib.sha256(message_id.encode()).hexdigest()[:32]}.eml"

    def compose(self, message_id: str, reply: EmailMessage) -> Path:
        """Persist the reply and its Message-ID before anything touches SMTP (working -> composed)."""
        reply_id = str(reply["Message-ID"] or "")
        if not reply_id:
            raise ValueError("reply has no Message-ID")
        path = self.eml_path(message_id)
        _write_atomic(path, reply.as_bytes(policy=policy.SMTP))
        if not self.ledger.transition(
            message_id, Status.WORKING, Status.COMPOSED, reply_message_id=reply_id, eml_path=str(path)
        ):
            path.unlink(missing_ok=True)
            raise StaleJob(f"{message_id} is no longer working; not composing")
        return path

    def send(self, message_id: str) -> bool:
        """SMTP the saved .eml (composed -> sent). False if SMTP refused it for good (row is then failed)."""
        job = self.ledger.get(message_id)
        if job is None or job.status != Status.COMPOSED or not job.eml_path:
            raise StaleJob(f"{message_id} is not composed")
        raw = Path(job.eml_path).read_bytes()
        sender, rcpts = _envelope(raw)
        try:
            self.transport.send(raw, sender, rcpts)
        except Exception as e:
            if not is_permanent(e):
                raise  # transient: the row stays composed and reconcile retries it
            log.error("smtp refused reply to %s for good: %s", message_id, e)
            self.ledger.transition(message_id, Status.COMPOSED, Status.FAILED, reason=f"smtp: {e}"[:500])
            return False
        self.ledger.transition(message_id, Status.COMPOSED, Status.SENT)
        return True

    def deliver(self, message_id: str, reply: EmailMessage) -> bool:
        self.compose(message_id, reply)
        return self.send(message_id)

    def settle(self, mailbox: SentLookup) -> list[str]:
        """Label and mark seen every original whose reply has been sent (sent -> done). Run on the IMAP thread."""
        done = []
        for job in self.ledger.in_states(Status.SENT):
            if job.uid:
                mailbox.mark_done(job.uid)
            if self.ledger.transition(job.message_id, Status.SENT, Status.DONE):
                done.append(job.message_id)
                # Sent Mail now holds the copy; keeping the .eml (up to ~24 MB each) would only fill the disk.
                if job.eml_path:
                    Path(job.eml_path).unlink(missing_ok=True)
        return done

    def retry_composed(self, mailbox: SentLookup, message_id: str, result: Reconciled | None = None) -> None:
        """Finish one composed job: confirm it if the reply is already in Sent Mail, else resend the saved .eml.
        Sent is checked first because SMTP can fail after the server has accepted the message. A transient
        SMTP error propagates and leaves the row composed."""
        result = result if result is not None else Reconciled()
        job = self.ledger.get(message_id)
        if job is None or job.status != Status.COMPOSED:
            return
        if job.reply_message_id and mailbox.sent_contains(job.reply_message_id):
            self.ledger.transition(job.message_id, Status.COMPOSED, Status.SENT)
            result.confirmed.append(job.message_id)
        elif job.eml_path and Path(job.eml_path).exists():
            if self.send(job.message_id):
                result.resent.append(job.message_id)
        else:
            # The ledger says composed but the file is gone (disk wiped?); start the job over.
            log.error("composed job %s lost its .eml; failing it", job.message_id)
            self.ledger.transition(job.message_id, Status.COMPOSED, Status.FAILED, reason="eml missing")

    def reconcile(
        self, mailbox: SentLookup, *, max_attempts: int = MAX_ATTEMPTS, reclaim_all: bool = False
    ) -> Reconciled:
        """Settle what a crash left behind. Call once at startup, before any worker runs.

        `reclaim_all` treats every working row as orphaned, not just expired ones: right for the sole
        instance at startup, so a crashed job is retried now instead of after its lease runs out.
        """
        result = Reconciled()
        for job in self.ledger.in_states(Status.COMPOSED):
            self.retry_composed(mailbox, job.message_id, result)
        orphans = self.ledger.in_states(Status.WORKING) if reclaim_all else self.ledger.expired_leases()
        for job in orphans:
            if job.attempts < max_attempts:
                if self.ledger.transition(job.message_id, Status.WORKING, Status.RECEIVED):
                    result.requeued.append(job.message_id)
            elif self.ledger.renew(job.message_id):
                result.exhausted.append(job.message_id)
        self.settle(mailbox)
        return result
