"""In-memory/on-disk stand-ins for the IMAP and SMTP servers, so mail code is testable without an account.

The Sent folder lives on disk so a separate (killed) process and the test can share it.
"""

from __future__ import annotations

from collections.abc import Sequence
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from pathlib import Path

from uarb_agent.mail.inbox import Inbound, parse_inbound


def make_email(
    *,
    sender: str = "Grader <grader@example.com>",
    to: str = "agent@gmail.com",
    subject: str = "Documents please",
    body: str = "Can you give me a list of Other Documents files from M12205?",
    message_id: str | None = "<orig-1@example.com>",
    headers: dict[str, str] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    return msg.as_bytes(policy=policy.SMTP)


class FakeServer:
    """One mailbox: an inbox with flags/labels, and a Sent folder of raw messages on disk."""

    def __init__(self, root: Path):
        self.sent_dir = root / "sent"
        self.sent_dir.mkdir(parents=True, exist_ok=True)
        self.inbox: dict[str, bytes] = {}
        self.seen: set[str] = set()
        self.labels: dict[str, set[str]] = {}
        self._next_uid = 1

    def deliver(self, raw: bytes) -> str:
        uid = str(self._next_uid)
        self._next_uid += 1
        self.inbox[uid] = raw
        return uid

    def sent(self) -> list[bytes]:
        return [p.read_bytes() for p in sorted(self.sent_dir.glob("*.eml"))]

    def sent_ids(self) -> list[str]:
        parser = BytesHeaderParser(policy=policy.default)
        return ["".join(str(parser.parsebytes(raw)["Message-ID"]).split()) for raw in self.sent()]  # unfold


class FakeTransport:
    """SMTP that 'delivers' by dropping the bytes into the server's Sent folder (Gmail saves a copy there)."""

    def __init__(self, server_root: Path, *, fail_with: BaseException | None = None):
        self.sent_dir = server_root / "sent"
        self.sent_dir.mkdir(parents=True, exist_ok=True)
        self.fail_with = fail_with
        self.calls: list[tuple[str, list[str]]] = []

    def send(self, raw: bytes, from_addr: str, to_addrs: Sequence[str]) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        n = len(list(self.sent_dir.glob("*.eml")))
        (self.sent_dir / f"{n:04d}.eml").write_bytes(raw)
        self.calls.append((from_addr, list(to_addrs)))


class FakeMailbox:
    """MailboxClient over a FakeServer."""

    def __init__(self, server: FakeServer, *, idle: bool = True):
        self.server = server
        self.idle = idle
        self.waits: list[float] = []
        self.done_calls: list[str] = []
        self.closed = False

    def fetch_unseen(self) -> list[Inbound]:
        return [
            parse_inbound(uid, raw)
            for uid, raw in self.server.inbox.items()
            if uid not in self.server.seen and "uarb-agent/done" not in self.server.labels.get(uid, set())
        ]

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.idle

    def mark_done(self, uid: str) -> None:
        self.done_calls.append(uid)
        self.server.labels.setdefault(uid, set()).add("uarb-agent/done")
        self.server.seen.add(uid)

    def sent_contains(self, message_id: str) -> bool:
        return message_id in self.server.sent_ids()

    def noop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
