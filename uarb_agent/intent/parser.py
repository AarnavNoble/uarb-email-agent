"""Email -> ParseResult. Deterministic rules decide; the optional LLM only fills a slot the rules left empty.

thread_state is the ledger's ThreadState (or any mapping/object with the same fields): matter, doc_type,
next_offset and pending, where pending looks like {"slot": "doc_type", "matter": "M12205"}.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import html2text

from uarb_agent.intent import llm_fallback
from uarb_agent.intent.aliases import find_types
from uarb_agent.models import (
    DOC_TYPES,
    MATTER_RE,
    Clarify,
    Command,
    CommandKind,
    DocType,
    ParseResult,
    Request,
)

MAX_REQUESTS = 3
MAX_DOCS = 10
MAX_TEXT = 10_000

LLMFn = Callable[[str], llm_fallback.LLMGuess | None]
_FROM_SETTINGS: Any = object()

FORMAT_HINT = (
    "Matter numbers look like M12205 (the letter M and five digits). I can fetch "
    + ", ".join(DOC_TYPES[:-1])
    + f" or {DOC_TYPES[-1]}."
)

# --- cleaning ---------------------------------------------------------------------------------

_REPLY_PREFIX = re.compile(r"(?i)^\s*(?:(?:re|fwd?|aw|tr|sv)\s*(?:\[\d+\])?\s*:\s*)+")
_DASHES = re.compile(r"[‐-―−]")
_SPACES = re.compile(r"[  -  ]")
_INVISIBLE = re.compile(r"[​-‍﻿]")

# Earliest match cuts the rest of the body: reply attributions, forwarded/original-message banners, signatures.
_CUTS = re.compile(
    r"(?im)"
    r"^\s*On\s[^\n]{0,200}?(?:\n[^\n]{0,200}?)?\swrote:\s*$"
    r"|^\s*Le\s[^\n]{0,200}?a\s+écrit\s*:\s*$"
    r"|^\s*-{2,}\s*(?:Original Message|Forwarded message)\s*-*\s*$"
    r"|^\s*Begin forwarded message:"
    r"|^\s*_{8,}\s*$"
    r"|^\s*\**From:\**\s.*\n\s*\**(?:Sent|Date|To):"
    r"|^\s*\\?--\s?$"
    r"|^\s*Sent from my \w+"
    r"|^\s*Get Outlook for \w+"
)
_GREETING = re.compile(r"(?i)^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening)|greetings)\b[^\n]{0,40}$")


def _normalise(text: str) -> str:
    text = _INVISIBLE.sub("", _SPACES.sub(" ", _DASHES.sub("-", text)))
    return text.replace("\r\n", "\n").replace("\r", "\n")


def html_to_text(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_links = True
    h.ignore_images = True
    h.ignore_emphasis = True
    return h.handle(html)


def strip_quotes(text: str) -> str:
    """Drop quoted history and signatures so only what the sender just wrote is parsed."""
    m = _CUTS.search(text)
    if m:
        text = text[: m.start()]
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(">")).strip()


def clean_body(body_text: str | None, body_html: str | None = None) -> str:
    raw = body_text if body_text and body_text.strip() else html_to_text(body_html or "")
    return strip_quotes(_normalise(raw)[:MAX_TEXT])


# --- mentions ---------------------------------------------------------------------------------

_BARE_MATTER = re.compile(r"(?i)\b(?:matter|docket)\s*(?:no\.?|number|num|#)?\s*[:#]?\s*(\d{5})\b")
_NEAR_MISS = re.compile(
    r"(?i)\bM[\s-]?(\d{3,4}|\d{6,7})\b|\b(?:matter|docket)\s*(?:no\.?|number|#)?\s*[:#]?\s*M?(\d{3,4}|\d{6,7})\b"
)
_LOOSE_FIVE = re.compile(r"(?<![\d-])(\d{5})(?![\d-])")


@dataclass(frozen=True)
class _Mention:
    start: int
    kind: str  # 'matter' | 'type' | 'invalid'
    value: str
    assumed: bool = False


@dataclass
class _Scan:
    mentions: list[_Mention]
    invalid: list[str]

    @property
    def matters(self) -> list[str]:
        return _unique(m.value for m in self.mentions if m.kind == "matter")

    @property
    def types(self) -> list[DocType]:
        return _unique(DocType(m.value) for m in self.mentions if m.kind == "type")

    @property
    def assumed(self) -> set[str]:
        return {m.value for m in self.mentions if m.assumed}


def _unique(items) -> list:
    return list(dict.fromkeys(items))


def _scan(text: str) -> _Scan:
    mentions: list[_Mention] = []
    spans: list[tuple[int, int]] = []
    for m in MATTER_RE.finditer(text):
        mentions.append(_Mention(m.start(), "matter", f"M{m.group(1)}"))
        spans.append(m.span())
    for m in _BARE_MATTER.finditer(text):
        if not any(m.start(1) < e and s < m.end(1) for s, e in spans):
            mentions.append(_Mention(m.start(1), "matter", f"M{m.group(1)}", assumed=True))
            spans.append(m.span(1))
    near = [m for m in _NEAR_MISS.finditer(text) if not any(m.start() < e and s < m.end() for s, e in spans)]
    # Kept as mentions so a type next to a bad number pairs with it (and is dropped), not with a good matter.
    mentions += [_Mention(m.start(), "invalid", m.group(0).strip()) for m in near]
    mentions += [_Mention(t.start, "type", str(t.doc_type)) for t in find_types(text)]
    return _Scan(sorted(mentions, key=lambda x: x.start), _unique(m.group(0).strip() for m in near))


def _pair(mentions: list[_Mention]) -> list[tuple[str | None, DocType | None]]:
    """Group mentions into alternating runs and pair neighbours: 'Exhibits for M1 and Key Docs for M2' pairs
    by order, 'Exhibits and Transcripts for M1 and M2' is a cross product. A trailing run of types joins the
    last pair ('... and the exhibits too')."""
    runs: list[tuple[str, list]] = []
    for m in mentions:
        kind, value = ("matter", None) if m.kind == "invalid" else (m.kind, m.value)
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(value)
        else:
            runs.append((kind, [value]))

    groups: list[tuple[list, list]] = []  # (matters, types); a None matter is an invalid number
    for i in range(0, len(runs), 2):
        a = runs[i]
        b = runs[i + 1] if i + 1 < len(runs) else None
        if b is None:
            if a[0] == "type" and groups and runs[i - 1][1][-1] is not None:
                groups[-1][1].extend(a[1])
            elif a[0] == "type" and groups:  # "... Also M1234 transcripts": they belong to the bad number
                groups.append(([None], a[1]))
            else:
                groups.append((a[1], []) if a[0] == "matter" else ([], a[1]))
        else:
            groups.append((a[1], b[1]) if a[0] == "matter" else (b[1], a[1]))

    pairs: list[tuple[str | None, DocType | None]] = []
    for matters, types in groups:
        for matter in _unique(matters) or [None]:
            for t in _unique(types) or [None]:
                pairs.append((matter, DocType(t) if t else None))
    pairs = _unique(pairs)
    typed = {m for m, t in pairs if t is not None}
    return [(m, t) for m, t in pairs if t is not None or m not in typed]


# --- limits, commands, auto-replies -----------------------------------------------------------

_NUM_WORDS = {
    w: i for i, w in enumerate(["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"], 1)
}
_NUM = r"(\d{1,3}|" + "|".join(_NUM_WORDS) + r")"
_LIMIT = re.compile(
    rf"(?i)\b(?:first|top|latest|last|newest|most recent|recent|only|just)\s+{_NUM}\b"
    rf"|\b{_NUM}\s+(?:of the\s+)?(?:(?:most recent|latest|newest|recent)\s+)?(?:[a-z]+\s+)?"
    r"(?:docs?|documents?|files?|pdfs?|exhibits?|transcripts?|recordings?)\b"
)
_ALL = re.compile(r"(?i)\b(?:all|every|everything)\b")
_THANKS = re.compile(
    r"(?i)^\W*(?:(?:ok(?:ay)?|got it|great|thanks?|thank you(?: (?:so|very) much)?|thx|ty|cheers|cool|perfect"
    r"|awesome|(?:much )?appreciated|this is|that'?s|that is|it|works|worked|all|so|very|much|again|for|the|help"
    r"|exactly what i needed)\W*)+$"
)


def batch_note(max_docs: int) -> str:
    """Said when someone asks for more than one batch; the worker drops it if the tab fitted in one anyway."""
    return f"I can send up to {max_docs} documents per email; reply MORE for the next batch."


def _limit(text: str, max_docs: int) -> tuple[int, list[str]]:
    m = _LIMIT.search(text)
    if m:
        raw = (m.group(1) or m.group(2)).lower()
        n = _NUM_WORDS.get(raw) or int(raw)
        if n > 0:
            if n > max_docs:
                return max_docs, [batch_note(max_docs)]
            return n, []
    if _ALL.search(text):
        return max_docs, [batch_note(max_docs)]
    return max_docs, []


_ACK = r"(?:(?:ok(?:ay)?|yes|yeah|great|thanks?|thank you|cool|perfect)[\s,!.]+)*"
_MORE = re.compile(
    rf"(?i)^{_ACK}(?:please\s+)?(?:(?:send|give)\s+(?:me\s+)?(?:the\s+)?)?(?:more|next)"
    r"(?:\s+(?:\d{1,2}|batch|page|set|ones|docs|documents|files|please|pls))*[\s.!?]*$"
    rf"|^{_ACK}(?:please\s+)?(?:(?:send|give)\s+(?:me\s+)?)?(?:the\s+)?(?:rest|remaining)"
    r"(?:\s+(?:ones|docs|documents|files|of them))?(?:\s+(?:please|pls))?[\s.!?]*$"
)
_GET = re.compile(
    r"(?i)^(?:please\s+)?(?:get|fetch|send)\s+(?:me\s+)?(?:(?:doc(?:ument)?|exhibit|file|item)\s*)?"
    r"(?:no\.?|number|#)?\s*([A-Z0-9][A-Z0-9()\-.]*)"
)
_DOC_NO = re.compile(r"(?i)\d{4,}|[A-Z]{1,3}-\d[\w()\-]*")
_WATCH = re.compile(r"(?i)^(?:please\s+|can you\s+|could you\s+)?(un)?watch\b(?!\s*out\b)")
_STOP_WATCH = re.compile(r"(?i)\b(?:stop|cancel|end)\s+(?:watching|monitoring)\b")
_NOTIFY = re.compile(r"(?i)\b(?:notify|alert|email|tell)\s+me\s+(?:when|if|whenever)\b[^\n]*\bnew\b")

_AUTO_SUBJECT = re.compile(
    r"(?i)^\s*(?:automatic reply|auto(?:matic)?[- ]?(?:reply|response)|out of (?:the )?office|ooo\b|auto:)"
)
_AUTO_BODY = re.compile(
    r"(?is)\b(?:out of (?:the )?office|on (?:vacation|leave|holiday)|away from (?:the|my) (?:office|desk))\b"
    r".{0,200}\b(?:until|return|back)\b"
)


def _first_line(text: str) -> str:
    for line in text.split("\n"):
        line = line.strip()
        if line and not _GREETING.match(line):
            return line
    return ""


def _command(line: str) -> tuple[CommandKind, str | None, int | None] | None:
    """(kind, doc_no, number) for a command on the first line, else None."""
    if _MORE.match(line):
        n = re.search(r"\d{1,2}", line)
        return CommandKind.MORE, None, int(n.group()) if n else None
    m = _GET.match(line)
    if m:
        token = m.group(1).rstrip(".")
        if _DOC_NO.fullmatch(token) and not MATTER_RE.fullmatch(token):
            return CommandKind.GET, token, None
    m = _WATCH.match(line)
    if m:
        return (CommandKind.UNWATCH if m.group(1) else CommandKind.WATCH), None, None
    if _STOP_WATCH.search(line):
        return CommandKind.UNWATCH, None, None
    if _NOTIFY.search(line):
        return CommandKind.WATCH, None, None
    return None


# --- thread state -----------------------------------------------------------------------------


def _get(state: Any, key: str) -> Any:
    if state is None:
        return None
    if isinstance(state, Mapping):
        return state.get(key)
    return getattr(state, key, None)


def _doc_type(value: Any) -> DocType | None:
    try:
        return DocType(value) if value else None
    except ValueError:
        return None


def _clarify_no_matter(text: str) -> Clarify:
    loose = _LOOSE_FIVE.search(text)
    lead = f"Did you mean M{loose.group(1)}? " if loose else "Which matter do you mean? "
    return Clarify(reason="no_matter", question=lead + FORMAT_HINT)


# --- entry point ------------------------------------------------------------------------------


def parse(
    subject: str | None,
    body_text: str | None,
    body_html: str | None = None,
    thread_state: Any = None,
    *,
    llm: LLMFn | None = _FROM_SETTINGS,
    max_docs: int = MAX_DOCS,
) -> ParseResult:
    """Parse one inbound email. `llm=None` forces rules only; by default the LLM runs iff settings enable it."""
    if llm is _FROM_SETTINGS:
        llm = _default_llm()
    subject = _normalise(subject or "")
    is_reply = bool(_REPLY_PREFIX.match(subject))
    subj = _REPLY_PREFIX.sub("", subject).strip()
    body = clean_body(body_text, body_html)

    if _AUTO_SUBJECT.match(subj):
        return ParseResult(clarify=Clarify(reason="auto_reply", question=""))

    cmd = _command(_first_line(body)) or (None if is_reply else _command(subj))
    if cmd:
        return _resolve_command(cmd, subj, body, thread_state, max_docs)

    pending = _get(thread_state, "pending") or {}
    if is_reply:
        scan, subj_scan = _scan(body), _scan(subj)
    else:
        scan, subj_scan = _scan(f"{subj}\n{body}"), None
    text = f"{subj}\n{body}"

    if not scan.matters and _AUTO_BODY.search(body):
        return ParseResult(clarify=Clarify(reason="auto_reply", question=""))

    limit, notes = _limit(body if is_reply else text, max_docs)
    pairs = _pair(scan.mentions)
    assumed = scan.assumed

    # Replies borrow the missing half from the thread: "Exhibits" after we asked which type, and so on.
    if is_reply and not scan.matters:
        matter = pending.get("matter") or _get(thread_state, "matter")
        if not matter and subj_scan and subj_scan.matters:
            matter = subj_scan.matters[0]
        if not matter and pending.get("slot") == "matter" and len(set(_LOOSE_FIVE.findall(body))) == 1:
            # We just asked which matter; a bare five-digit answer ("sorry, it's 12205") is the number.
            matter = f"M{_LOOSE_FIVE.search(body).group(1)}"
            assumed = {matter}
        if matter:
            types = scan.types or ([_doc_type(pending.get("doc_type"))] if pending.get("slot") == "matter" else [])
            if types:
                pairs = [(matter, t) for t in types]
            elif pending.get("slot") == "doc_type" and body and not _THANKS.match(body):
                return _maybe_llm_type(llm, body, matter, limit, notes)
            elif not scan.invalid:
                # A bare "thanks" needs no answer; anything else we could not read gets told what we can do.
                reason = "no_request" if not body or _THANKS.match(body) else "unclear_followup"
                q = f"Reply with a document type for {matter}, or MORE for the next batch. {FORMAT_HINT}"
                return ParseResult(clarify=Clarify(reason=reason, question=q))
    elif is_reply and pending.get("slot") == "matter" and not scan.types:
        fill = _doc_type(pending.get("doc_type"))
        if fill:
            pairs = [(m, fill if t is None else t) for m, t in pairs]

    for bad in scan.invalid:
        if scan.matters:
            notes.append(f"{bad} isn't a valid matter number (M plus five digits), so I skipped it.")

    requests = [
        Request(
            matter=m,
            doc_type=t,
            limit=limit,
            assumed_notes=[f"I assumed you meant {m}."] if m in assumed else [],
        )
        for m, t in pairs
        if m is not None
    ]

    if not requests:
        if scan.invalid:
            q = f"{scan.invalid[0]} doesn't look like a matter number. {FORMAT_HINT}"
            return ParseResult(clarify=Clarify(reason="invalid_matter_like", question=q), notes=notes)
        guess = llm(text) if llm else None
        if guess and guess.matter:
            types = scan.types or [guess.doc_type]
            requests = [
                Request(matter=guess.matter, doc_type=t, limit=limit, assumed_notes=[f"I read this as {guess.matter}."])
                for t in types
            ]
        else:
            return ParseResult(clarify=_clarify_no_matter(text), notes=notes)
    elif llm and not scan.types and len(requests) == 1:
        guess = llm(text)
        if guess and guess.doc_type and guess.matter in (None, requests[0].matter):
            requests[0].doc_type = guess.doc_type
            requests[0].assumed_notes.append(f"I took this as a request for {guess.doc_type}.")

    if len(requests) > MAX_REQUESTS:
        rest = ", ".join(f"{r.matter} {r.doc_type or 'summary'}" for r in requests[MAX_REQUESTS:])
        notes.append(f"I handle up to {MAX_REQUESTS} requests per email, so I didn't process: {rest}.")
        requests = requests[:MAX_REQUESTS]
    return ParseResult(requests=requests, notes=notes)


def _maybe_llm_type(llm: LLMFn | None, body: str, matter: str, limit: int, notes: list[str]) -> ParseResult:
    guess = llm(body) if llm else None
    if guess and guess.doc_type and guess.matter in (None, matter):
        return ParseResult(requests=[Request(matter=matter, doc_type=guess.doc_type, limit=limit)], notes=notes)
    q = f"I didn't recognise that document type. For {matter} I can fetch " + ", ".join(DOC_TYPES) + "."
    return ParseResult(clarify=Clarify(reason="unknown_type", question=q), notes=notes)


def _resolve_command(
    cmd: tuple[CommandKind, str | None, int | None], subj: str, body: str, state: Any, max_docs: int
) -> ParseResult:
    kind, doc_no, n = cmd
    if kind is CommandKind.MORE:
        matter, doc_type = _get(state, "matter"), _doc_type(_get(state, "doc_type"))
        if not (matter and doc_type):
            q = "I'm not sure what to continue from. Tell me the matter and document type. " + FORMAT_HINT
            return ParseResult(command=Command(kind=kind), clarify=Clarify(reason="no_context", question=q))
        req = Request(
            matter=matter, doc_type=doc_type, limit=min(n or max_docs, max_docs), offset=_get(state, "next_offset") or 0
        )
        return ParseResult(command=Command(kind=kind), requests=[req])

    matters = _scan(body).matters or _scan(subj).matters
    matter = matters[0] if matters else _get(state, "matter")
    args = [doc_no] if doc_no else []
    if not matter:
        return ParseResult(command=Command(kind=kind, args=args), clarify=_clarify_no_matter(body))
    return ParseResult(command=Command(kind=kind, args=[*args, matter]))


def _default_llm() -> LLMFn | None:
    from uarb_agent.config import get_settings

    return llm_fallback.guess if get_settings().llm_active else None


__all__ = ["parse", "batch_note", "clean_body", "strip_quotes", "html_to_text", "FORMAT_HINT"]
