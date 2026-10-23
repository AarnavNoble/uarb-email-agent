"""Reply headers that keep Gmail, Outlook and Apple Mail threading the conversation, plus thread identity."""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Sequence
from email.message import EmailMessage, Message
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path

_RE_PREFIX = re.compile(r"(?i)^\s*re\s*:")
_MSGID = re.compile(r"<[^<>\s]+>")
MAX_REFERENCES = 20  # keep the root plus the most recent ids; some servers reject very long headers
# Copy of our Message-ID that survives senders (like the Gmail API) which may assign their own.
REPLY_ID_HEADER = "X-Agent-Reply-ID"


def message_ids(value: str | None) -> list[str]:
    return _MSGID.findall(value or "")


def thread_key(msg: Message) -> str:
    """The id of the first message in the conversation: References root, else In-Reply-To, else its own id."""
    for header in ("References", "In-Reply-To", "Message-ID"):
        ids = message_ids(str(msg.get(header, "")))
        if ids:
            return ids[0]
    return ""


def sender_address(msg: Message) -> str:
    return parseaddr(str(msg.get("From", "")))[1].lower()


def reply_recipient(msg: Message) -> str:
    """Reply-To wins over From, as a mail client would do."""
    for header in ("Reply-To", "From"):
        addrs = [a for _, a in getaddresses([str(v) for v in msg.get_all(header, [])]) if "@" in a]
        if addrs:
            return addrs[0]
    raise ValueError("message has no usable Reply-To or From address")


def reply_subject(subject: str | None) -> str:
    subject = " ".join((subject or "").split())
    if _RE_PREFIX.match(subject):
        return subject
    return f"Re: {subject}" if subject else "Re: your request"


def reply_references(msg: Message) -> list[str]:
    refs = message_ids(str(msg.get("References", "")))
    if not refs:  # some clients only set In-Reply-To
        refs = message_ids(str(msg.get("In-Reply-To", "")))
    own = message_ids(str(msg.get("Message-ID", "")))
    refs = [r for r in refs if r not in own] + own
    if len(refs) > MAX_REFERENCES:
        refs = refs[:1] + refs[-(MAX_REFERENCES - 1) :]
    return refs


def build_reply(
    original: Message,
    *,
    from_addr: str,
    text: str,
    html: str | None = None,
    attachments: Sequence[Path] = (),
    trace_id: str | None = None,
    from_name: str = "UARB Email Agent",
) -> EmailMessage:
    """A threaded, auto-reply-marked response to `original`. The Message-ID is fresh; callers persist it."""
    reply = EmailMessage()
    reply["From"] = formataddr((from_name, from_addr))
    reply["To"] = reply_recipient(original)
    reply["Subject"] = reply_subject(str(original.get("Subject", "")))
    reply["Date"] = formatdate(localtime=True)
    reply["Message-ID"] = make_msgid(idstring="uarb", domain=from_addr.rpartition("@")[2] or None)
    reply[REPLY_ID_HEADER] = reply["Message-ID"]

    original_ids = message_ids(str(original.get("Message-ID", "")))
    if original_ids:
        reply["In-Reply-To"] = original_ids[0]
    refs = reply_references(original)
    if refs:
        reply["References"] = " ".join(refs)

    # RFC 3834: mark ourselves as automatic so other robots (and our own guards) do not answer us.
    reply["Auto-Submitted"] = "auto-replied"
    reply["X-Auto-Response-Suppress"] = "All"
    if trace_id:
        reply["X-Agent-Trace"] = trace_id

    reply.set_content(text)
    if html is not None:
        reply.add_alternative(html, subtype="html")
    for path in attachments:
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        reply.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return reply
