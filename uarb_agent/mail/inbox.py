"""Receiving side: an IMAP client behind a small protocol, and the IDLE/poll loop that feeds the worker.

Messages are fetched with BODY.PEEK so nothing is marked \\Seen until the reply has gone out
(`MailboxClient.mark_done`). If the process dies mid-job the message is still unseen and gets picked up again;
the ledger keeps that from turning into a second reply.
"""

from __future__ import annotations

import contextlib
import email
import hashlib
import imaplib
import logging
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from typing import Protocol

from imap_tools import AND, NOT, MailBox
from imap_tools.errors import ImapToolsError
from imap_tools.query import Header

from uarb_agent.mail.threading import REPLY_ID_HEADER, message_ids, sender_address, thread_key

log = logging.getLogger(__name__)

DONE_LABEL = "uarb-agent/done"
GMAIL_SENT = "[Gmail]/Sent Mail"
IDLE_TIMEOUT_S = 55  # Gmail drops IDLE after ~29 min and NATs drop quiet sockets sooner; re-arm well inside both
POLL_INTERVAL_S = 20


@dataclass(frozen=True)
class Inbound:
    uid: str
    message_id: str
    msg: EmailMessage
    raw: bytes

    @property
    def sender(self) -> str:
        return sender_address(self.msg)

    @property
    def subject(self) -> str:
        return str(self.msg.get("Subject", ""))

    @property
    def thread_key(self) -> str:
        return thread_key(self.msg) or self.message_id


def parse_inbound(uid: str, raw: bytes) -> Inbound:
    msg = email.message_from_bytes(raw, policy=policy.default)
    ids = message_ids(str(msg.get("Message-ID", "")))
    # Without a Message-ID we still need a stable ledger key; the content hash is the same on every redelivery.
    mid = ids[0] if ids else f"<no-id-{hashlib.sha256(raw).hexdigest()[:24]}@uarb-agent.invalid>"
    return Inbound(uid=uid, message_id=mid, msg=msg, raw=raw)


class MailboxClient(Protocol):
    def fetch_unseen(self) -> list[Inbound]:
        """Unseen, not-yet-done messages in the inbox, without marking them seen."""
        ...

    def wait(self, timeout: float) -> bool:
        """IDLE until the server reports a change or `timeout` passes. False, at once, if IDLE is unsupported."""
        ...

    def mark_done(self, uid: str) -> None:
        """Label the message done and mark it \\Seen. Called only after the reply is in Sent Mail."""
        ...

    def sent_contains(self, message_id: str) -> bool:
        """True if a message with this Message-ID is already in the Sent folder."""
        ...

    def noop(self) -> None: ...

    def close(self) -> None: ...


class ImapMailbox:
    """imap-tools over IMAPS. Gmail extras (labels, X-GM-RAW) are used only when `gmail` is true."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        gmail: bool | None = None,
        sent_folder: str = GMAIL_SENT,
        timeout: float = 60,
    ):
        self.host, self.user, self._password = host, user, password
        self.gmail = host.endswith("gmail.com") if gmail is None else gmail
        self.sent_folder = sent_folder
        self.timeout = timeout
        self._box: MailBox | None = None

    def connect(self) -> ImapMailbox:
        self._box = MailBox(self.host, timeout=self.timeout).login(self.user, self._password, initial_folder="INBOX")
        return self

    @property
    def box(self) -> MailBox:
        if self._box is None:
            raise RuntimeError("not connected")
        return self._box

    def _unseen_criteria(self):
        if self.gmail:
            return AND(NOT(gmail_label=DONE_LABEL), seen=False)
        return AND(seen=False)

    def fetch_unseen(self) -> list[Inbound]:
        found = []
        for m in self.box.fetch(self._unseen_criteria(), mark_seen=False, bulk=True):
            found.append(parse_inbound(m.uid, m.obj.as_bytes(policy=policy.compat32)))
        return found

    def wait(self, timeout: float) -> bool:
        if "IDLE" not in self.box.client.capabilities:
            return False
        self.box.idle.wait(timeout=timeout)
        return True

    def mark_done(self, uid: str) -> None:
        if self.gmail:
            # STORE directly: imap-tools' flag() also EXPUNGEs, which we never want here.
            self.box.client.uid("STORE", uid, "+X-GM-LABELS", f'("{DONE_LABEL}")')
        self.box.client.uid("STORE", uid, "+FLAGS", "(\\Seen)")

    def sent_contains(self, message_id: str) -> bool:
        self.box.folder.set(self.sent_folder)
        try:
            # The Gmail API may replace our Message-ID, so also look for the copy every reply carries.
            return bool(
                self.box.uids(AND(header=Header("Message-ID", message_id)))
                or self.box.uids(AND(header=Header(REPLY_ID_HEADER, message_id)))
            )
        finally:
            self.box.folder.set("INBOX")

    def noop(self) -> None:
        self.box.client.noop()

    def close(self) -> None:
        if self._box is not None:
            with contextlib.suppress(Exception):  # already dead; nothing useful to do
                self._box.logout()
            self._box = None


# What counts as "the connection broke, reconnect" rather than a bug.
CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    socket.timeout,
    ssl.SSLError,
    imaplib.IMAP4.error,
    ImapToolsError,  # includes login failures: a revoked app password retries slowly instead of crash-looping
    EOFError,
)


def backoff_delays(base: float = 2.0, cap: float = 300.0):
    delay = base
    while True:
        yield delay
        delay = min(delay * 2, cap)


def run_inbox(
    connect: Callable[[], MailboxClient],
    on_batch: Callable[[MailboxClient, list[Inbound]], None],
    stop: threading.Event,
    *,
    idle_timeout: float = IDLE_TIMEOUT_S,
    poll_interval: float = POLL_INTERVAL_S,
    on_healthy: Callable[[], None] | None = None,
) -> None:
    """Feed new mail to `on_batch` until `stop` is set.

    Every (re)connect starts with a catch-up fetch, so mail that arrived while we were down is not missed.
    IDLE is re-armed every `idle_timeout` seconds; without IDLE support we poll every `poll_interval`.
    Connection failures reconnect with exponential backoff; `on_batch` errors propagate (they are bugs).
    """
    delays = backoff_delays()
    while not stop.is_set():
        client = None
        try:
            client = connect()
            delays = backoff_delays()
            while not stop.is_set():
                batch = client.fetch_unseen()
                if batch:
                    on_batch(client, batch)
                client.noop()
                if on_healthy:
                    on_healthy()
                if not client.wait(idle_timeout):
                    stop.wait(poll_interval)
        except CONNECTION_ERRORS as e:
            delay = next(delays)
            log.warning("imap connection lost (%s: %s); reconnecting in %.0fs", type(e).__name__, e, delay)
            stop.wait(delay)
        finally:
            if client is not None:
                client.close()
