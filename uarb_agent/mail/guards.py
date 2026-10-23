"""Decide whether an inbound message deserves a reply at all.

Header checks catch bounces, auto-replies, list mail and our own messages (RFC 3834 and common practice);
rate checks stop two robots, or one enthusiastic human, from turning the agent into a mail cannon.
Every check returns a short machine-readable reason, or None to let the message through.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Iterable
from email.message import Message
from email.utils import parseaddr
from typing import Protocol

from uarb_agent.mail.threading import sender_address

PER_SENDER_PER_HOUR = 10
PER_THREAD = 20

_AUTO_LOCALPART = re.compile(r"(?i)^(mailer-daemon|postmaster|no[-_.]?reply|do[-_.]?not[-_.]?reply|bounces?)([+-].*)?$")
_BULK_PRECEDENCE = {"bulk", "junk", "list", "auto_reply"}
# Set by vacation responders. X-Auto-Response-Suppress is deliberately absent: ordinary Outlook mail can carry it.
_AUTOREPLY_HEADERS = ("X-Autoreply", "X-Autorespond", "X-Autoresponder", "X-Failed-Recipients")
_LIST_HEADERS = ("List-Id", "List-Unsubscribe", "List-Post")
CANARY_HEADER = "X-Agent-Canary"


def _header(msg: Message, name: str) -> str:
    return str(msg.get(name, "") or "").strip()


def is_canary(msg: Message, canary_token: str | None) -> bool:
    """A self-addressed test message carrying the shared secret, which the agent answers like any other."""
    token = _header(msg, CANARY_HEADER)
    return bool(canary_token and token and hmac.compare_digest(token, canary_token))


def loop_reason(msg: Message, own_addresses: Iterable[str], *, canary_token: str | None = None) -> str | None:
    """Why this message must be ignored on its headers alone, or None."""
    sender = sender_address(msg)
    if not sender or "@" not in sender:
        return "no_sender"
    own = {a.lower() for a in own_addresses if a}
    if sender in own and not is_canary(msg, canary_token):
        return "own_address"

    auto = _header(msg, "Auto-Submitted").lower()
    if auto and auto.split(";")[0].strip() != "no":
        return "auto_submitted"
    if _header(msg, "Precedence").lower() in _BULK_PRECEDENCE:
        return "precedence"
    for name in _AUTOREPLY_HEADERS:
        if name in msg:
            return "autoreply_header"
    for name in _LIST_HEADERS:
        if name in msg:
            return "mailing_list"
    if "Return-Path" in msg and parseaddr(_header(msg, "Return-Path"))[1] == "":
        return "null_return_path"  # '<>' marks a bounce or other automatic notification
    if _AUTO_LOCALPART.match(sender.partition("@")[0]):
        return "automated_sender"
    ctype = msg.get_content_type()
    if ctype == "multipart/report" or ctype.startswith("message/delivery-status"):
        return "delivery_report"
    return None


class ReplyCounter(Protocol):
    """The slice of the ledger the rate checks need."""

    def now(self) -> float: ...

    def replies_to_sender_since(self, sender: str, since: float) -> int: ...

    def replies_in_thread(self, thread_key: str) -> int: ...


def rate_reason(
    counter: ReplyCounter,
    sender: str,
    thread_key: str,
    *,
    per_hour: int = PER_SENDER_PER_HOUR,
    per_thread: int = PER_THREAD,
) -> str | None:
    """Check before claiming a job, so the message being judged is not counted against itself."""
    if counter.replies_to_sender_since(sender.lower(), counter.now() - 3600) >= per_hour:
        return "sender_rate_limited"
    if thread_key and counter.replies_in_thread(thread_key) >= per_thread:
        return "thread_cap"
    return None


def screen(
    msg: Message,
    counter: ReplyCounter,
    own_addresses: Iterable[str],
    *,
    thread_key: str,
    canary_token: str | None = None,
) -> str | None:
    reason = loop_reason(msg, own_addresses, canary_token=canary_token)
    if reason or is_canary(msg, canary_token):  # test runs are not rate limited
        return reason
    return rate_reason(counter, sender_address(msg), thread_key)
