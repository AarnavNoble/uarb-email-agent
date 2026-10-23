"""Pure parsers for what the connector's page scripts extract.

The page scripts return text boxes with their on-screen geometry (`Box`); everything that decides what
a box means lives here so it can be tested offline against captured snapshots."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import TypedDict

from uarb_agent.models import DOC_TYPES, DocRow, DocType, LayoutChanged, MatterMetadata, normalize_matter


class Box(TypedDict):
    t: str
    x: float
    y: float
    w: float
    h: float


COUNT_RE = re.compile(r"^\s*(" + "|".join(re.escape(t) for t in DOC_TYPES) + r")\s*-\s*(\d+)\s*$")
FOUND_RE = re.compile(r"Found Count:\s*(\d+)")
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")
BUTTON_TEXT = {"GO GET IT", "Preview"}
HEARING_TABS = {DocType.TRANSCRIPTS, DocType.RECORDINGS}
HEARING_FORMATS = {"pdf", "mp3", "mp4", "wav", "m4a", "doc", "docx"}  # a bare format field on hearing rows


def parse_date(text: str | None) -> dt.date | None:
    """Site dates are US style, MM/DD/YYYY. Blank means no date; anything else is a layout problem."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError as e:
        raise LayoutChanged(f"unparseable date {text!r}") from e


def parse_counts(button_texts: list[str]) -> dict[DocType, int]:
    """All five tab counts from the tab button captions ('Exhibits - 13'), in site order."""
    found: dict[DocType, int] = {}
    for text in button_texts:
        m = COUNT_RE.match(text)
        if m:
            found.setdefault(DocType(m.group(1)), int(m.group(2)))
    missing = [t for t in DOC_TYPES if t not in found]
    if missing:
        raise LayoutChanged(f"tab counts missing for {', '.join(missing)}")
    return {t: found[t] for t in DOC_TYPES}


def parse_found_count(text: str) -> int | None:
    """'Found Count: N' above the grid. Not every tab layout shows it (Key Documents does not)."""
    m = FOUND_RE.search(text)
    return int(m.group(1)) if m else None


# --- metadata ---------------------------------------------------------------------------------------

# Header label text (lowercased, whitespace collapsed) -> MatterMetadata field. The final-date label
# differs between the overview ('Decision Date') and tab layouts ('Date Final Submission(s)').
LABEL_FIELDS = {
    "matter no": "matter_no",
    "status": "status",
    "title - description": "title",
    "type": "type",
    "category": "category",
    "date received": "date_received",
    "decision date": "date_final",
    "date final submissions": "date_final",
    "date final submission": "date_final",
    "outcome": "outcome",
}
# Positional fallback: the first value line left to right, then the second line.
TOP_LINE = ("matter_no", "title", "type", "date_received", "date_final", "outcome")
SECOND_LINE = ("status", "category")
LINE_TOLERANCE = 15  # px; values on one visual line start within this of each other
COLUMN_TOLERANCE = 30  # px; a value may start a little left of its label


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _label_slots(text: str) -> list[str] | None:
    """A header label names one field, or two stacked fields ('Matter No' over 'Status')."""
    whole = _norm(text)
    if whole in LABEL_FIELDS:
        return [LABEL_FIELDS[whole]]
    lines = [_norm(line) for line in text.splitlines() if line.strip()]
    if lines and all(line in LABEL_FIELDS for line in lines):
        return [LABEL_FIELDS[line] for line in lines]
    return None


def _value_lines(values: list[Box]) -> tuple[list[Box], list[Box]]:
    if not values:
        return [], []
    top_y = min(v["y"] for v in values)
    top = sorted((v for v in values if v["y"] - top_y < LINE_TOLERANCE), key=lambda v: v["x"])
    rest = sorted((v for v in values if v["y"] - top_y >= LINE_TOLERANCE), key=lambda v: v["x"])
    return top, rest


def _columns(labels: list[tuple[Box, list[str]]]) -> list[tuple[Box, list[str]]]:
    """Stack labels sharing an x (one element per line, or one element with a line break) into a column."""
    columns: list[tuple[Box, list[str]]] = []
    for box, slots in sorted(labels, key=lambda lab: (lab[0]["x"], lab[0]["y"])):
        if columns and abs(columns[-1][0]["x"] - box["x"]) < 5:
            columns[-1][1].extend(slots)
        else:
            columns.append((box, list(slots)))
    return columns


def _by_label(labels: list[tuple[Box, list[str]]], top: list[Box], second: list[Box]) -> dict[str, str]:
    columns = _columns(labels)
    out: dict[str, str] = {}
    for line_no, line in enumerate((top, second)):
        for v in line:
            owners = [slots for box, slots in columns if box["x"] <= v["x"] + COLUMN_TOLERANCE]
            if owners and line_no < len(owners[-1]):
                out.setdefault(owners[-1][line_no], v["t"])
    return out


def parse_metadata(boxes: list[Box]) -> MatterMetadata:
    """Matter header -> MatterMetadata, by label first and by position if the labels do not resolve.

    `boxes` are the visible text boxes above the tab bar: header labels and their values, in any order.
    Values sit below their label's column; a two-line label maps line 1 to the first value line."""
    labels: list[tuple[Box, list[str]]] = []
    values: list[Box] = []
    for b in boxes:
        slots = _label_slots(b["t"])
        if slots:
            labels.append((b, slots))
        elif b["t"].strip():
            values.append(b)
    if labels:
        header_bottom = max(lab["y"] for lab, _ in labels)
        values = [v for v in values if v["y"] > header_bottom]
    top, second = _value_lines(values)

    fields = _by_label(labels, top, second) if labels else {}
    if not {"matter_no", "title"} <= fields.keys():
        fields = dict(zip(TOP_LINE, (v["t"] for v in top), strict=False))
        fields.update(zip(SECOND_LINE, (v["t"] for v in second), strict=False))
    return _validate(fields)


def _validate(fields: dict[str, str]) -> MatterMetadata:
    clean = {k: " ".join(v.split()) for k, v in fields.items()}
    try:
        matter_no = normalize_matter(clean.get("matter_no", ""))
    except Exception as e:
        raise LayoutChanged(f"matter number not found in header: {clean.get('matter_no')!r}") from e
    if not clean.get("title"):
        raise LayoutChanged("matter title not found in header")
    return MatterMetadata(
        matter_no=matter_no,
        title=clean["title"],
        type=clean.get("type", ""),
        category=clean.get("category", ""),
        status=clean.get("status") or None,
        outcome=clean.get("outcome") or None,
        date_received=parse_date(clean.get("date_received")),
        date_final=parse_date(clean.get("date_final")),
    )


# --- document rows ----------------------------------------------------------------------------------


def _hearing_row(top: list[Box], lower: list[Box], date: Box | None, tab: DocType) -> DocRow:
    """Transcripts and Recordings rows have no document number: just a date and free text (a hearing title,
    'September 12, 2022' + 'Evening Session', or the panel's names), and several rows can share a date or a
    first field. The id is the tab letter, the date and a short hash of the row text, e.g. 'T-20220912-3fa1':
    unique within the tab, stable across listings, and shaped like a document number so GET accepts it."""
    texts = [" ".join(b["t"].split()) for b in top]
    fmt = next((t for t in texts if t.lower() in HEARING_FORMATS), None)
    title = " - ".join(t for t in texts if t is not fmt)
    day = parse_date(date["t"]) if date else None
    digest = hashlib.sha1(f"{day} {' | '.join(texts)}".encode()).hexdigest()[:4]
    return DocRow(
        doc_no=f"{str(tab)[0]}-{day:%Y%m%d}-{digest}" if day else f"{str(tab)[0]}-{digest}",
        title=title,
        date=day,
        ext=f".{fmt.lower()}" if fmt else None,
        security=lower[0]["t"].strip() if lower else None,
        tab=tab,
    )


def parse_row(boxes: list[Box], tab: DocType) -> DocRow | None:
    """One grid row -> DocRow. Each tab is its own layout, so fields are found by shape, not by id:
    date and extension by regex, doc no is the top-left box, the title is the widest other top box,
    and the security level is the left-most box on the lower line. None for a row with no doc no.
    Transcripts and Recordings have no doc no column; see `_hearing_row`."""
    boxes = [b for b in boxes if b["t"].strip() and b["t"].strip() not in BUTTON_TEXT]
    date = next((b for b in boxes if DATE_RE.match(b["t"].strip())), None)
    ext = next((b for b in boxes if EXT_RE.match(b["t"].strip())), None)
    rest = [b for b in boxes if b is not date and b is not ext]
    if not rest:
        return None
    top_y = min(b["y"] for b in rest)
    top = sorted((b for b in rest if b["y"] - top_y < LINE_TOLERANCE), key=lambda b: b["x"])
    lower = sorted((b for b in rest if b["y"] - top_y >= LINE_TOLERANCE), key=lambda b: b["x"])
    if tab in HEARING_TABS:
        return _hearing_row(top, lower, date, tab)
    titles = sorted(top[1:], key=lambda b: -b["w"])
    return DocRow(
        doc_no=top[0]["t"].strip(),
        title=" ".join(titles[0]["t"].split()) if titles else "",
        date=parse_date(date["t"]) if date else None,
        ext=ext["t"].strip().lower() if ext else None,
        security=lower[0]["t"].strip() if lower else None,
        tab=tab,
    )
