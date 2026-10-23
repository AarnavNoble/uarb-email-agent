"""Turn MatterReports and parser outcomes into plain-text + HTML reply bodies.

Every number in a reply comes from a MatterReport, or from an argument the caller passes explicitly
(a rate-limit time, the counts before a new filing). `check_numbers` enforces that on every render.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from uarb_agent.models import (
    DOC_TYPES,
    AgentError,
    Clarify,
    DocRow,
    DocType,
    EmptyTab,
    InvalidMatterNumber,
    MatterNotFound,
    MatterReport,
    Request,
)

SITE = "uarb.novascotia.ca"
MATTER_EXAMPLE = "M01234"
BATCH = 10  # documents per reply; matches Settings.max_docs

_SINGULAR = {
    DocType.EXHIBITS: "Exhibit",
    DocType.KEY_DOCUMENTS: "Key Document",
    DocType.OTHER_DOCUMENTS: "Other Document",
    DocType.TRANSCRIPTS: "Transcript",
    DocType.RECORDINGS: "Recording",
}

Block = dict[str, Any]


class ReplyCheckFailed(AgentError):
    """A rendered reply mentions a number that the report does not contain."""

    def __init__(self, numbers: set[str], **kw):
        super().__init__(f"reply mentions numbers not in the report: {', '.join(sorted(numbers))}", **kw)
        self.numbers = numbers


@dataclass(frozen=True)
class Reply:
    text: str
    html: str
    subject: str | None = None  # only for messages that start a thread (new-filing alerts)


@dataclass
class Outcome:
    """One request from an email and what came of it: a report, or the error that stopped it."""

    request: Request
    report: MatterReport | None = None
    error: AgentError | None = None


# ---------------------------------------------------------------- wording helpers


def label(n: int, doc_type: DocType) -> str:
    return str(doc_type) if n != 1 else _SINGULAR[doc_type]


def n_docs(n: int, doc_type: DocType) -> str:
    return f"{n} {label(n, doc_type)}"


def oxford(items: Sequence[str], conj: str = "and") -> str:
    items = list(items)
    if len(items) <= 2:
        return f" {conj} ".join(items)
    return f"{', '.join(items[:-1])}, {conj} {items[-1]}"


def long_date(d: dt.date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def short_date(d: dt.date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def human_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------- self-check

_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _norm(token: str) -> str:
    token = token.replace(",", "")
    return token if "." in token else (token.lstrip("0") or "0")


def numbers_in(text: str) -> set[str]:
    return {_norm(t) for t in _NUM.findall(text)}


def _date_numbers(d: dt.date | None) -> set[str]:
    return set() if d is None else {str(d.day), str(d.month), str(d.year)}


def allowed_numbers(report: MatterReport) -> set[str]:
    """Every number the report itself states: counts, dates, and digits inside its text fields."""
    md = report.metadata
    texts = [md.matter_no, md.title, md.type, md.category, md.status or "", md.outcome or "", report.trace_id]
    texts += [f"{r.doc_no} {r.title}" for r in report.rows]
    texts += [f"{f.doc_no} {f.title}" for f in report.files]
    texts += [f"{s.doc_no} {s.title} {s.reason}" for s in report.skipped]
    out = numbers_in(" ".join(texts))
    out |= _date_numbers(md.date_received) | _date_numbers(md.date_final)
    for r in report.rows:
        out |= _date_numbers(r.date)
    out |= {str(n) for n in report.counts.values()}
    out.add(str(report.offset))
    return out


def check_numbers(text: str, allowed: set[str]) -> None:
    """Raise ReplyCheckFailed if `text` contains a number outside `allowed`."""
    stray = numbers_in(text) - allowed
    if stray:
        raise ReplyCheckFailed(stray)


# ---------------------------------------------------------------- blocks


def _about(report: MatterReport) -> dict[str, Any]:
    md = report.metadata
    return {
        "matter": md.matter_no,
        "title": md.title.strip().rstrip("."),
        "type": md.type.strip(),
        "category": md.category.strip(),
        "received": long_date(md.date_received) if md.date_received else None,
        "final": long_date(md.date_final) if md.date_final else None,
        "counts": oxford([n_docs(report.counts.get(t, 0), t) for t in DOC_TYPES]),
    }


def _available(report: MatterReport, exclude: DocType | None = None) -> list[str]:
    """Non-empty types, largest first, as 'Other Documents (43)'."""
    found = [(n, t) for t in DOC_TYPES if t != exclude and (n := report.counts.get(t, 0)) > 0]
    found.sort(key=lambda nt: -nt[0])  # stable, so ties keep site order
    return [f"{t} ({n})" for n, t in found]


def _block(kind: str, allowed: Iterable[str] = (), **fields: Any) -> Block:
    return {"kind": kind, "allowed": set(allowed), "heading": None, "notes": [], **fields}


def _report_block(report: MatterReport, batch: int) -> Block:
    base = {"p": _about(report), "matter": report.metadata.matter_no}
    allowed = allowed_numbers(report)
    t = report.requested
    if t is None:
        return _block("summary", allowed | {str(batch)}, available=_available(report), batch=batch, **base)

    total = report.counts.get(t, 0)
    if total == 0:
        return _block("empty_tab", allowed, tab=str(t), available=_available(report, exclude=t), **base)
    if report.offset >= total and not report.files:
        all_of = f"the only {label(1, t)}" if total == 1 else f"all {n_docs(total, t)}"
        return _block("past_end", allowed, all_of=all_of, **base)

    got = len(report.downloaded)
    position = {r.doc_no: i for i, r in enumerate(report.rows)}
    attempted = [f.doc_no for f in report.files] + [s.doc_no for s in report.skipped]
    touched = [position[d] for d in attempted if d in position]
    end = max(touched) + 1 if touched else report.offset + len(report.files) + len(report.skipped)
    end = max(min(end, total), report.offset + 1)
    start = report.offset + 1

    if got == 0:
        mode = "none"
    elif report.offset == 0 and got == total:
        mode = {1: "only", 2: "both"}.get(total, "all")
    elif report.offset == 0:
        mode = "some"
    else:
        mode = "range" if got == end - report.offset else "range_partial"
    window = f"documents {start}-{end}" if end > start else f"document {start}"

    dates = {r.doc_no: r.date for r in report.rows}
    tried = len(report.files) + len(report.skipped)
    files, extra = [], {str(got), str(start), str(end), str(tried)}
    for f in report.downloaded:
        meta = [short_date(d)] if (d := dates.get(f.doc_no)) else []
        if f.size:
            meta.append(human_size(f.size))
            extra |= numbers_in(meta[-1])
        files.append({"doc_no": f.doc_no, "title": f.title, "meta": ", ".join(meta)})
    problems = [{"doc_no": f.doc_no, "title": f.title, "reason": "download failed"} for f in report.files if not f.ok]
    problems += [{"doc_no": s.doc_no, "title": s.title, "reason": s.reason} for s in report.skipped]

    more = None
    if got and end < total:
        nxt_end = min(end + batch, total)
        more = f"documents {end + 1}-{nxt_end}" if nxt_end > end + 1 else f"document {end + 1}"
        extra |= {str(end + 1), str(nxt_end)}

    seconds = None
    if report.timings:
        seconds = max(1, round(report.timings.get("total", sum(report.timings.values()))))
        extra.add(str(seconds))

    return _block(
        "docs",
        allowed | extra,
        mode=mode,
        got=got,
        tried=tried,
        total=total,
        label=label(total, t),
        window=window,
        files=files,
        problems=problems,
        more=more,
        others=[str(o) for o in DOC_TYPES if o != t and report.counts.get(o, 0) > 0],
        seconds=seconds,
        site=SITE,
        **base,
    )


def _error_block(request: Request, err: AgentError) -> Block:
    matter = getattr(err, "matter", None) or request.matter
    hint = numbers_in(MATTER_EXAMPLE)
    if isinstance(err, MatterNotFound):
        return _block("not_found", numbers_in(matter) | hint, matter=matter, example=MATTER_EXAMPLE)
    if isinstance(err, InvalidMatterNumber):
        return _block("invalid", numbers_in(err.value) | hint, value=err.value, example=MATTER_EXAMPLE)
    if isinstance(err, EmptyTab):
        return _block("no_docs", numbers_in(matter), matter=matter, tab=str(err.tab))
    trace = err.trace_id
    return _block("site_down", numbers_in(f"{matter} {trace or ''}"), matter=matter, trace_id=trace, site=SITE)


def _heading(request: Request) -> str:
    return f"{request.matter}: {request.doc_type}" if request.doc_type else f"{request.matter}: summary"


# ---------------------------------------------------------------- rendering

_TEMPLATES = Path(__file__).parent / "templates"


def _env(autoescape: bool) -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES),
        autoescape=autoescape,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["oxford"] = oxford
    return env


_TEXT_ENV = _env(autoescape=False)
_HTML_ENV = _env(autoescape=True)


def _tidy_text(text: str) -> str:
    lines = [re.sub(r" {2,}", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def _tidy_html(html: str) -> str:
    return re.sub(r"\n\s*\n+", "\n", html).strip() + "\n"


def _visible_text(html: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", html))


def _render(
    blocks: list[Block],
    *,
    notes: Sequence[str] = (),
    not_processed: Sequence[Request] = (),
    footer: bool = True,
    subject: str | None = None,
    extra_allowed: Iterable[str] = (),
) -> Reply:
    allowed = set(extra_allowed) | numbers_in(" ".join(notes))
    for b in blocks:
        allowed |= b["allowed"] | numbers_in(" ".join(b["notes"]))
    skipped = [_heading(r) for r in not_processed]
    if skipped:
        allowed |= numbers_in(" ".join(skipped)) | {str(len(blocks))}
    ctx = {
        "blocks": blocks,
        "multi": len(blocks) > 1,
        "notes": list(notes),
        "not_processed": skipped,
        "cap": len(blocks),
        "footer": footer,
    }
    text = _tidy_text(_TEXT_ENV.get_template("email.txt.j2").render(ctx))
    html = _tidy_html(_HTML_ENV.get_template("email.html.j2").render(ctx))
    check_numbers(text, allowed)
    check_numbers(_visible_text(html), allowed)
    return Reply(text=text, html=html, subject=subject)


def render_outcomes(
    outcomes: Sequence[Outcome],
    *,
    notes: Sequence[str] = (),
    not_processed: Sequence[Request] = (),
    batch: int = BATCH,
) -> Reply:
    """The main reply: one section per request (a single request reads as a plain letter)."""
    blocks = []
    for o in outcomes:
        if o.report is not None:
            b = _report_block(o.report, batch)
        elif o.error is not None:
            b = _error_block(o.request, o.error)
        else:
            raise ValueError("an Outcome needs a report or an error")
        b["heading"] = _heading(o.request)
        b["notes"] = list(o.request.assumed_notes)
        blocks.append(b)
    return _render(blocks, notes=notes, not_processed=not_processed)


def render_report(report: MatterReport, *, notes: Sequence[str] = (), batch: int = BATCH) -> Reply:
    """Shortcut for the common single-request reply."""
    request = Request(matter=report.metadata.matter_no, doc_type=report.requested, offset=report.offset)
    return render_outcomes([Outcome(request, report=report)], notes=notes, batch=batch)


def render_clarify(clarify: Clarify, report: MatterReport | None = None, *, batch: int = BATCH) -> Reply:
    """Ask the sender for what is missing. With a report (unknown type), show the matter summary first."""
    allowed = numbers_in(f"{MATTER_EXAMPLE} {clarify.question}") | {str(batch)}
    fields: dict[str, Any] = {
        "reason": clarify.reason,
        "question": clarify.question,
        "example": MATTER_EXAMPLE,
        "types": oxford([str(t) for t in DOC_TYPES], "or"),
        "batch": batch,
        "p": None,
    }
    if report is not None:
        allowed |= allowed_numbers(report)
        fields["p"] = _about(report)
    return _render([_block("clarify", allowed, **fields)])


def render_watch_confirm(report: MatterReport) -> Reply:
    return _render([_block("watch", allowed_numbers(report), p=_about(report), matter=report.metadata.matter_no)])


def render_unwatch(matter: str, *, was_watching: bool = True) -> Reply:
    return _render([_block("unwatch", numbers_in(matter), matter=matter, was_watching=was_watching)])


def render_new_filing(report: MatterReport, before: dict[DocType, int], new_rows: Sequence[DocRow] = ()) -> Reply:
    """Alert for a watched matter whose counts moved. `before` is the snapshot taken at the last check."""
    matter = report.metadata.matter_no
    changes = [
        {"tab": str(t), "was": before.get(t, 0), "now": report.counts.get(t, 0)}
        for t in DOC_TYPES
        if before.get(t, 0) != report.counts.get(t, 0)
    ]
    rows = [
        {"doc_no": r.doc_no, "title": r.title, "tab": str(r.tab), "date": r.date and short_date(r.date)}
        for r in new_rows
    ]
    allowed = allowed_numbers(report) | {str(n) for n in before.values()}
    allowed |= numbers_in(" ".join(f"{r.doc_no} {r.title}" for r in new_rows))
    for r in new_rows:
        allowed |= _date_numbers(r.date)
    block = _block("new_filing", allowed, matter=matter, title=report.metadata.title, changes=changes, rows=rows)
    return _render([block], subject=f"New filing on {matter}")


def render_ack(requests: Sequence[Request]) -> Reply:
    """Sent only when a job is slow; the full reply follows in the same thread."""
    items = [f"{r.matter} {r.doc_type}" if r.doc_type else f"{r.matter} (summary)" for r in requests]
    return _render([_block("ack", numbers_in(" ".join(items)), what=oxford(items))], footer=False)


def render_rate_limited(until: dt.datetime, per_hour: int) -> Reply:
    when = until.strftime("%H:%M %Z").strip()
    return _render([_block("rate_limited", numbers_in(when) | {str(per_hour)}, until=when, per_hour=per_hour)])


def render_help(*, batch: int = BATCH) -> Reply:
    types = oxford([str(t) for t in DOC_TYPES], "or")
    block = _block("help", numbers_in(MATTER_EXAMPLE) | {str(batch)}, example=MATTER_EXAMPLE, types=types, batch=batch)
    return _render([block], footer=False)
