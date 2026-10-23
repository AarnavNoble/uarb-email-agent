"""What the Board decided: amounts, date and conditions from a Board Order or Decision PDF, each with its page.

Deterministic regexes over pymupdf text. A field is filled only when a pattern the Board's own drafting style uses
matches; anything else stays None (or empty) - never a guess. Verified against M12205 (Order 102674, Decision 99761),
M12383 (Order 101689, Decision and Order 100105) and M10431 (Order 89360, Decision 88655).

Layout the patterns rely on:
- caption / recital: '... APPLICATION by X for approval of <project> for a cost of $64,769,000'
- operative part: 'The Board approves|orders ...' (orders) or the last CONCLUSION heading (decisions), ending at
  'DATED at Halifax, Nova Scotia, this 8th day of July 2026.'
- decision cover page: 'DECISION DATE: October 23, 2025' and 'DECISION: Application approved with ...'
"""

from __future__ import annotations

import bisect
import calendar
import datetime as dt
import functools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pymupdf

from uarb_agent.cache.store import Store
from uarb_agent.models import DocRow, DocType, MatterReport

MAX_CONDITIONS = 25
_MIN_TEXT_CHARS = 200  # less than this over the whole file means a scan (no text layer)
_OPERATIVE_WINDOW = 8000  # chars after the operative start to search when there is no DATED line

_MONTHS = {m: i for i, m in enumerate(calendar.month_name) if m}

# ------------------------------------------------------------------ patterns

# A whole amount: never a prefix of a longer number, never a rate ('$0.125 per kWh').
_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s*(million|billion))?(?![.,]?\d)(?!\s*(?:per\b|/))"
_FURNITURE = re.compile(r"^\s*(?:Document:\s*\d+|-\s*\d+\s*-)\s*$")  # running header and '- 2 -' page numbers
_HEADING = re.compile(r"\b(?:CONCLUSION|SUMMARY OF (?:MAJOR )?FINDINGS(?: AND DIRECTIVES)?)\b(?!\s*\.{3})")
_OPERATIVE = re.compile(
    r"\bThe Board,?\s+(?:(?:hereby|therefore|also|accordingly),?\s+)?"
    r"(approves|orders|denies|dismisses|grants|rejects)\b"
)
_VERDICT = re.compile(
    r"\bThe Board,?\s+(?:(?:hereby|therefore|also|accordingly),?\s+)?(approves|denies|dismisses|grants|rejects)\b"
    r"([^.]{0,300})"
)
_PAST = {
    "approves": "approved",
    "denies": "denied",
    "dismisses": "dismissed",
    "grants": "granted",
    "rejects": "rejected",
}
_APPLIED = re.compile(
    r"(?:for (?:the )?approval of|applied|application)[^.$]{0,300}?\b(?:cost|amount|expenditure|total|sum) of "
    + _MONEY,
    re.I,
)
_APPROVED = re.compile(
    r"\bapprov(?:es|ed)\b[^.$]{0,300}?\b(?:cost|amount|expenditure|total|sum|budget) of " + _MONEY, re.I
)
_DATED = re.compile(
    r"\bDATED at [^,]{1,40},[^,]{0,30}, this (\d{1,2})\s*(?:st|nd|rd|th)?\s*d\S{0,4}?\s*of\s+([A-Za-z]+),?\s+(\d{4})"
)
_DECISION_DATE = re.compile(r"\bDECISION DATE:\s*([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})")
_SUMMARY = re.compile(r"\bDECISION:\s*(.{10,400}?\.)(?=\s+[A-Z(\[]|\s*$)")

_SEGMENT_SPLIT = re.compile(r"(?<=[.;:\]])\s+(?=(?:\d{1,2}\.|[a-zA-Z]\)|•|\[\d+\])\s|[A-Z])")
_ITEM = re.compile(r"^(?:\d{1,2}\.|[a-zA-Z]\)|•)\s*")  # '1.', 'a)', bullet
_PARAGRAPH = re.compile(r"^\[\d+\]\s*")  # decision paragraph numbers, '[88]'
_DIRECTIVE = re.compile(
    r"\b(?:is|are) (?:hereby )?directed\b|\bdirects\b|\bsubject to\b"
    r"|\b(?:shall|must|is to|are to) (?:continue to )?(?:file|submit|provide|report)\b",
    re.I,
)
_DIRECTIVE_INTRO = re.compile(r"\b(?:directed|directs)\b[^.]*:$", re.I)  # '... is directed as follows:'
_REPORTING = re.compile(
    r"\b((?:semi-annual|annual|quarterly|monthly)(?:\s*\([^)]{0,30}\))?\s+(?:[\w-]+\s+){0,3}?(?:reports?|updates?))\b",
    re.I,
)
_LABEL_WORDS = 14


# ------------------------------------------------------------------ result types


@dataclass(frozen=True)
class Cited[T]:
    value: T
    page: int  # 1-based page of the PDF the value was read from
    quote: str  # the matched text, so a reader can check it
    literal: str = ""  # how the document writes the value ('$4.75 million'); replies show this, never a reformat


@dataclass(frozen=True)
class Outcome:
    doc_no: str
    title: str
    applied: Cited[Decimal] | None = None  # dollars applied for, from the caption or recital
    approved: Cited[Decimal] | None = None  # dollars the operative part approves
    date: Cited[dt.date] | None = None  # date the order/decision was signed
    disposition: Cited[str] | None = None  # 'approved', 'approved in principle', 'denied', 'dismissed', ...
    summary: Cited[str] | None = None  # a decision's own one-sentence 'DECISION:' line
    conditions: tuple[Cited[str], ...] = ()  # directions and 'subject to' terms in the operative part

    @property
    def found(self) -> bool:
        """Anything worth telling a reader: a verdict, an approved amount or conditions."""
        return bool(self.approved or self.disposition or self.summary or self.conditions)

    @property
    def pages(self) -> list[int]:
        cited = [self.applied, self.approved, self.date, self.disposition, self.summary, *self.conditions]
        return sorted({c.page for c in cited if c is not None})


# ------------------------------------------------------------------ which documents


def is_outcome_title(title: str) -> bool:
    """'Board Order', 'Board Decision', 'Board Amended Decision and Order' yes; hearing orders, notices, letters no."""
    t = title.lower()
    return bool(re.search(r"\b(?:order|decision)\b", t)) and not re.search(r"hearing|notice|letter|procedural", t)


# ------------------------------------------------------------------ text


class _Text:
    """All pages joined into one whitespace-normalised string, remembering where each page starts."""

    def __init__(self, pages: Sequence[str]):
        parts, self.starts, pos = [], [], 0
        for page in pages:
            lines = [ln for ln in page.splitlines() if not _FURNITURE.match(ln)]
            clean = " ".join(re.sub(r"\xad\s*", "-", " ".join(lines)).split())  # soft hyphen at a line break
            self.starts.append(pos)
            parts.append(clean)
            pos += len(clean) + 1
        self.text = " ".join(parts)

    def page(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)


def read_pages(path: Path) -> list[str] | None:
    """Text of each page, or None for a non-PDF, an unreadable file or a scan without a text layer."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        with pymupdf.open(path) as doc:
            pages = [p.get_text() for p in doc]
    except Exception:
        return None
    return pages if sum(len(p.strip()) for p in pages) >= _MIN_TEXT_CHARS else None


# ------------------------------------------------------------------ fields


def _money(m: re.Match[str]) -> Decimal:
    whole, frac, scale = m.group(m.re.groups - 2), m.group(m.re.groups - 1), m.group(m.re.groups)
    value = Decimal(whole.replace(",", "") + (f".{frac}" if frac else ""))
    if scale:
        value *= 1_000_000 if scale.lower() == "million" else 1_000_000_000
    return value


def _money_cite(text: _Text, m: re.Match[str]) -> Cited[Decimal]:
    literal = " ".join(m.group(0)[m.group(0).rindex("$") :].split())
    return replace(_cite(text, m, _money(m)), literal=literal)


def _date(day: str, month: str, year: str) -> dt.date | None:
    try:
        return dt.date(int(year), _MONTHS[month.capitalize()], int(day))
    except (KeyError, ValueError):
        return None


def _cite[T](text: _Text, m: re.Match[str], value: T) -> Cited[T]:
    return Cited(value=value, page=text.page(m.start()), quote=m.group(0)[-300:])


def _operative(text: _Text) -> tuple[int, int] | None:
    """Span of the operative part: after the last CONCLUSION-style heading, else from the first 'The Board
    approves/orders' (recitals use the past tense), up to the DATED line."""
    headings = list(_HEADING.finditer(text.text))
    if headings:
        start = headings[-1].end()
    elif m := _OPERATIVE.search(text.text):
        start = m.start()
    else:
        return None
    dated = _DATED.search(text.text, start)
    return start, dated.start() if dated else min(len(text.text), start + _OPERATIVE_WINDOW)


def _signed(text: _Text, after: int) -> Cited[dt.date] | None:
    for m in (_DATED.search(text.text, after), _DATED.search(text.text), _DECISION_DATE.search(text.text)):
        if m is None:
            continue
        d = _date(*m.groups()) if m.re is _DATED else _date(m.group(2), m.group(1), m.group(3))
        if d:
            return _cite(text, m, d)
    return None


def _verdict(text: _Text, start: int, end: int) -> Cited[str] | None:
    m = _VERDICT.search(text.text, start, end)
    if not m:
        return None
    value = _PAST[m.group(1)]
    if value == "approved" and re.search(r"\bin principle\b", m.group(2)):
        value = "approved in principle"
    return _cite(text, m, value)


def _segments(text: _Text, start: int, end: int) -> Iterable[tuple[int, str]]:
    pos = start
    for seg in _SEGMENT_SPLIT.split(text.text[start:end]):
        at = text.text.index(seg, pos) if seg else pos
        pos = at + len(seg)
        if seg.strip():
            yield at, seg.strip()


def _conditions(text: _Text, start: int, end: int) -> tuple[Cited[str], ...]:
    """Directive sentences, plus every item of a list introduced by '... is directed as follows:'. Inside such a
    list, unmarked sentences continue the current item; a '[88]' paragraph number ends the list."""
    out: list[Cited[str]] = []
    listing = False
    for at, seg in _segments(text, start, end):
        if _PARAGRAPH.match(seg):
            listing = False
        clean = _PARAGRAPH.sub("", _ITEM.sub("", seg))
        if _DIRECTIVE_INTRO.search(seg):
            listing = True
        elif listing and not _ITEM.match(seg) and out:
            last = out[-1]
            out[-1] = replace(last, value=f"{last.value} {clean}", quote=f"{last.value} {clean}"[:300])
        elif listing or _DIRECTIVE.search(seg):
            out.append(Cited(value=clean, page=text.page(at), quote=clean[:300]))
    return tuple(out[:MAX_CONDITIONS])


def parse_outcome(pages: Sequence[str], *, doc_no: str, title: str) -> Outcome:
    """Pure extraction over page texts (index 0 = page 1)."""
    text = _Text(pages)
    span = _operative(text)
    head_end = span[0] if span else len(text.text)

    cover_end = text.starts[3] if len(text.starts) > 3 else len(text.text)  # caption, recital, 'DECISION:'
    applied = _APPLIED.search(text.text, 0, min(head_end, cover_end))
    summary = _SUMMARY.search(text.text, 0, cover_end)
    out = Outcome(
        doc_no=doc_no,
        title=title,
        applied=_money_cite(text, applied) if applied else None,
        date=_signed(text, head_end),
        summary=_cite(text, summary, summary.group(1)) if summary else None,
    )
    if span is None:
        return out
    approved = _APPROVED.search(text.text, *span)
    return replace(
        out,
        approved=_money_cite(text, approved) if approved else None,
        disposition=_verdict(text, *span),
        conditions=_conditions(text, *span),
    )


def extract_outcome(path: Path, *, doc_no: str, title: str) -> Outcome | None:
    """Outcome of one downloaded PDF, or None when it has no readable text."""
    pages = read_pages(Path(path))
    return None if pages is None else parse_outcome(pages, doc_no=doc_no, title=title)


def best_outcome(outcomes: Iterable[Outcome | None]) -> Outcome | None:
    """The most informative outcome; ties go to the earlier one (pass newest first). None if nothing was found."""
    best, best_score = None, 0
    for o in outcomes:
        if o is None:
            continue
        score = 4 * bool(o.approved) + 2 * bool(o.disposition or o.summary) + bool(o.conditions)
        if score > best_score:
            best, best_score = o, score
    return best


# ------------------------------------------------------------------ rendering


def format_money(value: Decimal) -> str:
    """Display form of a computed amount. Extracted amounts are shown as `Cited.literal` instead."""
    return f"${value:,.0f}" if value == value.to_integral_value() else f"${value:,.2f}"


def condition_label(text: str) -> str:
    """Short phrase for a reply line: the reporting duty if there is one, else the 'subject to' clause, else the
    opening words."""
    if m := _REPORTING.search(text):
        return m.group(1).lower()
    if (i := text.lower().find("subject to")) >= 0:
        text = text[i:]
    words = text.rstrip(" .;:,").split()
    return " ".join(words[:_LABEL_WORDS]) + ("..." if len(words) > _LABEL_WORDS else "")


def _pages(pages: list[int]) -> str:
    if len(pages) == 1:
        return f"p. {pages[0]}"
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"pp. {pages[0]}-{pages[-1]}"
    return "pp. " + ", ".join(map(str, pages))


def _long_date(d: dt.date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _as_written(c: Cited[Decimal]) -> str:
    return c.literal or format_money(c.value)


def _verdict_text(o: Outcome) -> tuple[str | None, list[Cited]]:
    """The approved amount if stated, else the decision's own one-line summary, else the bare disposition."""
    if o.approved:
        text = f"approved {_as_written(o.approved)}"
        if o.applied:
            return f"{text} of {_as_written(o.applied)} requested", [o.applied, o.approved]
        return text, [o.approved]
    if o.summary:
        text = o.summary.value.rstrip(".")
        if text[:2].istitle():  # 'Application approved ...' reads better mid-sentence as 'application approved'
            text = text[0].lower() + text[1:]
        return text, [o.summary]
    if o.disposition:
        return o.disposition.value, [o.disposition]
    return None, []


def render_line(o: Outcome | None, *, max_conditions: int = 2) -> str | None:
    """One reply line, e.g. 'Board decision: approved $59,143,000 of $64,769,000 requested (Board Order 102674, July 8,
    2026, pp. 1-2); conditions: semi-annual (every six months) reports.' None when nothing was found. Pages cite
    only what the line states."""
    if o is None or not o.found:
        return None
    verdict, cited = _verdict_text(o)
    labels: dict[str, Cited[str]] = {}
    for c in o.conditions:
        labels.setdefault(condition_label(c.value), c)
    shown = sorted(labels, key=lambda x: not _REPORTING.fullmatch(x))[:max_conditions]  # reporting duties first
    cited += [labels[x] for x in shown]
    where = [
        f"{o.title} {o.doc_no}",
        _long_date(o.date.value) if o.date else "",
        _pages(sorted({c.page for c in cited})),
    ]
    cite = ", ".join(x for x in where if x)
    more = len(labels) - len(shown)
    conds = "; ".join(shown) + (f"; and {more} more" if more else "")
    if verdict:
        return f"Board decision: {verdict} ({cite})" + (f"; conditions: {conds}." if conds else ".")
    return f"Board decision ({cite}): conditions: {conds}."


# ------------------------------------------------------------------ from the cache

# Board Orders show up under Key Documents and, on many matters, Other Documents too.
_OUTCOME_TABS = (DocType.KEY_DOCUMENTS, DocType.OTHER_DOCUMENTS)


@functools.lru_cache(maxsize=256)
def _extract_cached(path: str, size: int, doc_no: str, title: str) -> Outcome | None:
    """Blobs are content-addressed and immutable, so (path, size) identifies the bytes."""
    return extract_outcome(Path(path), doc_no=doc_no, title=title)


def decision_for(report: MatterReport, store: Store) -> Outcome | None:
    """The most informative outcome among the matter's Board Orders and Decisions that are already in the cache
    (including any this fetch just downloaded). Reads no network; rows come from the report or cached listings."""
    matter = report.metadata.matter_no
    rows: dict[str, DocRow] = {}
    for tab in _OUTCOME_TABS:
        listed = report.rows if report.requested is tab else store.get_rows(matter, tab, stale_ok=True)
        for row in listed or []:
            if is_outcome_title(row.title):
                rows.setdefault(row.doc_no, row)
    found: list[Outcome | None] = []
    for row in sorted(rows.values(), key=lambda r: r.date or dt.date.min, reverse=True):
        files = next((fs for t in _OUTCOME_TABS if (fs := store.get_doc(matter, t, row.doc_no))), [])
        found += [_extract_cached(str(f.path), f.size, row.doc_no, row.title) for f in files]
    return best_outcome(found)
