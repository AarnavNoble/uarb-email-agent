"""Shared contract: every component speaks these types and raises these errors."""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class DocType(StrEnum):
    """The five document tabs on a matter page, valued exactly as the site labels them."""

    EXHIBITS = "Exhibits"
    KEY_DOCUMENTS = "Key Documents"
    OTHER_DOCUMENTS = "Other Documents"
    TRANSCRIPTS = "Transcripts"
    RECORDINGS = "Recordings"


# Site order; replies always list counts in this order.
DOC_TYPES: tuple[DocType, ...] = tuple(DocType)

MATTER_RE = re.compile(r"(?i)\bM[\s-]?(\d{5})\b")


def normalize_matter(raw: str) -> str:
    """'m-12205' / 'M 12205' / 'M12205' -> 'M12205'; anything else raises InvalidMatterNumber."""
    m = MATTER_RE.fullmatch(raw.strip())
    if not m:
        raise InvalidMatterNumber(raw)
    return f"M{m.group(1)}"


class MatterMetadata(BaseModel):
    matter_no: str
    title: str
    type: str
    category: str
    status: str | None = None
    outcome: str | None = None
    date_received: dt.date | None = None
    date_final: dt.date | None = None


class DocRow(BaseModel):
    doc_no: str  # not always numeric: exhibits look like 'H-4(C)-i'
    title: str
    date: dt.date | None = None
    ext: str | None = None  # with the dot, e.g. '.pdf'
    security: str | None = None  # 'Public' / 'Confidential' as shown on the site
    tab: DocType


class FileResult(BaseModel):
    doc_no: str
    title: str
    path: Path | None = None
    size: int = 0
    sha256: str | None = None
    ok: bool = False
    error: str | None = None


class Skipped(BaseModel):
    doc_no: str
    title: str
    reason: str


class MatterReport(BaseModel):
    metadata: MatterMetadata
    counts: dict[DocType, int]
    requested: DocType | None = None
    offset: int = 0
    rows: list[DocRow] = Field(default_factory=list)  # every row listed for the requested tab, site order
    files: list[FileResult] = Field(default_factory=list)  # downloads attempted, in selection order
    skipped: list[Skipped] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)  # stage name -> seconds
    trace_id: str

    @property
    def downloaded(self) -> list[FileResult]:
        return [f for f in self.files if f.ok]


class Request(BaseModel):
    matter: str
    doc_type: DocType | None = None  # None means summary only
    limit: int = 10
    offset: int = 0
    assumed_notes: list[str] = Field(default_factory=list)  # e.g. "I assumed you meant M12205"


class Clarify(BaseModel):
    reason: str  # machine-readable, e.g. 'no_matter', 'invalid_matter_like', 'unknown_type'
    question: str


class CommandKind(StrEnum):
    MORE = "MORE"
    WATCH = "WATCH"
    UNWATCH = "UNWATCH"
    GET = "GET"


class Command(BaseModel):
    kind: CommandKind
    args: list[str] = Field(default_factory=list)  # e.g. matter for WATCH, doc_no for GET


class ParseResult(BaseModel):
    requests: list[Request] = Field(default_factory=list)
    clarify: Clarify | None = None
    command: Command | None = None
    notes: list[str] = Field(default_factory=list)


class AgentError(Exception):
    """Base for every expected failure; trace_id points at data/traces/<trace_id>/ when one was saved."""

    def __init__(self, message: str = "", *, trace_id: str | None = None):
        super().__init__(message)
        self.trace_id = trace_id


class InvalidMatterNumber(AgentError):
    def __init__(self, value: str, **kw):
        super().__init__(f"not a matter number: {value!r}", **kw)
        self.value = value


class MatterNotFound(AgentError):
    def __init__(self, matter: str, **kw):
        super().__init__(f"matter {matter} not found", **kw)
        self.matter = matter


class EmptyTab(AgentError):
    def __init__(self, matter: str, tab: DocType, **kw):
        super().__init__(f"{matter} has no {tab}", **kw)
        self.matter = matter
        self.tab = tab


class LayoutChanged(AgentError):
    """The page did not look the way the connector expects (selectors, labels or counts failed to parse)."""


class UnexpectedModal(AgentError):
    def __init__(self, text: str, **kw):
        super().__init__(f"unexpected modal: {text}", **kw)
        self.text = text


class SiteUnavailable(AgentError):
    """UARB unreachable, timed out, or erroring."""
