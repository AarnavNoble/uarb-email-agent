"""Fixed inputs for the reply golden tests. Values mirror M12205 as captured during recon; none are live."""

from __future__ import annotations

import datetime as dt

from uarb_agent.models import (
    Clarify,
    DocRow,
    DocType,
    EmptyTab,
    FileResult,
    LayoutChanged,
    MatterMetadata,
    MatterNotFound,
    MatterReport,
    Request,
    SiteUnavailable,
    Skipped,
)
from uarb_agent.reply import render as r

OD = DocType.OTHER_DOCUMENTS
KD = DocType.KEY_DOCUMENTS

META = MatterMetadata(
    matter_no="M12205",
    title="Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000",
    type="Water",
    category="Capital Expenditure Approvals",
    status="Closed",
    outcome="Directions given",
    date_received=dt.date(2025, 4, 7),
    date_final=dt.date(2025, 10, 23),
)

COUNTS = {
    DocType.EXHIBITS: 13,
    DocType.KEY_DOCUMENTS: 6,
    DocType.OTHER_DOCUMENTS: 43,
    DocType.TRANSCRIPTS: 0,
    DocType.RECORDINGS: 0,
}

_TITLES = [
    "Board Order",
    "HRWC (Board) Letter - Reply Submission",
    "CA (Board) Letter - Comments on Compliance Filing",
    "HRWC Compliance Filing",
    "Board Letter - Process Update",
    "Intervenor Request - Halifax Chamber",
    "HRWC Response to Information Requests",
    "Board Information Requests to HRWC",
    "Notice of Public Hearing",
    "Affidavit of Publication",
    "CA Letter - Consultant Retained",
    "HRWC Application Cover Letter",
]


def other_rows(n: int = 43) -> list[DocRow]:
    """n Other Documents, newest first, numbered down from 102674; every 6th row is Confidential."""
    start = dt.date(2026, 7, 8)
    return [
        DocRow(
            doc_no=str(102674 - 17 * i),
            title=_TITLES[i % len(_TITLES)],
            date=start - dt.timedelta(days=5 * i),
            ext=".pdf",
            security="Confidential" if i % 6 == 5 else "Public",
            tab=OD,
        )
        for i in range(n)
    ]


def files_for(rows: list[DocRow], sizes: list[int] | None = None) -> list[FileResult]:
    sizes = sizes or [170_000 + 9_000 * i for i in range(len(rows))]
    return [
        FileResult(doc_no=x.doc_no, title=x.title, size=s, sha256="0" * 64, ok=True)
        for x, s in zip(rows, sizes, strict=True)
    ]


def report(**kw) -> MatterReport:
    base = {"metadata": META, "counts": dict(COUNTS), "trace_id": "20261023T141205-a1b2c3"}
    return MatterReport(**(base | kw))


def happy() -> MatterReport:
    """Ten newest Public Other Documents; the Confidential row in the window is skipped."""
    rows = other_rows()
    public = [x for x in rows if x.security == "Public"][:10]
    conf = [x for x in rows[:11] if x.security == "Confidential"]
    return report(
        requested=OD,
        rows=rows,
        files=files_for(public),
        skipped=[Skipped(doc_no=x.doc_no, title=x.title, reason="confidential on the UARB site") for x in conf],
        timings={"open": 4.1, "list": 3.6, "download": 19.8, "total": 28.2},
    )


def key_docs_all() -> MatterReport:
    rows = [
        DocRow(doc_no=str(102300 + i), title=f"Key filing {chr(65 + i)}", date=dt.date(2025, 5, 1 + i), tab=KD)
        for i in range(6)
    ]
    return report(requested=KD, rows=rows, files=files_for(rows), timings={"total": 9.4})


def single_doc() -> MatterReport:
    rows = [DocRow(doc_no="102301", title="Board Decision", date=dt.date(2025, 10, 23), tab=KD)]
    return report(
        counts=COUNTS | {KD: 1}, requested=KD, rows=rows, files=files_for(rows, [2_400_000]), timings={"total": 6.0}
    )


def more_page() -> MatterReport:
    rows = other_rows()
    window = rows[10:20]
    return report(requested=OD, offset=10, rows=rows, files=files_for(window), timings={"total": 21.0})


def partial_with_failures() -> MatterReport:
    """MORE batch where one download failed and one file was over the email budget."""
    rows = other_rows()
    window = rows[20:30]
    files = files_for(window[:8])
    files.append(FileResult(doc_no=window[8].doc_no, title=window[8].title, ok=False, error="Timeout 20000ms"))
    skipped = [Skipped(doc_no=window[9].doc_no, title=window[9].title, reason="too large to email (21.3 MB)")]
    return report(requested=OD, offset=20, rows=rows, files=files, skipped=skipped)


def all_failed() -> MatterReport:
    rows = other_rows()
    files = [FileResult(doc_no=x.doc_no, title=x.title, ok=False, error="boom") for x in rows[:2]]
    return report(requested=OD, rows=rows, files=files)


def summary_only() -> MatterReport:
    return report(requested=None)


def no_final_date() -> MatterReport:
    return report(requested=None, metadata=META.model_copy(update={"date_final": None}))


def empty_tab() -> MatterReport:
    return report(requested=DocType.TRANSCRIPTS)


def past_end() -> MatterReport:
    return report(requested=OD, offset=43, rows=other_rows())


def reqs(*pairs) -> list[Request]:
    return [Request(matter=m, doc_type=t) for m, t in pairs]


def m11111() -> MatterReport:
    meta = MatterMetadata(
        matter_no="M11111",
        title="Nova Scotia Power Inc. - General Rate Application",
        type="Electricity",
        category="Rates",
        date_received=dt.date(2024, 1, 15),
    )
    counts = dict.fromkeys(COUNTS, 0) | {DocType.EXHIBITS: 2, DocType.TRANSCRIPTS: 1}
    rows = [
        DocRow(doc_no="H-4(C)-i", title="NSPI Evidence", date=dt.date(2024, 2, 1), tab=DocType.EXHIBITS),
        DocRow(doc_no="H-3", title="NSPI Application", date=dt.date(2024, 1, 15), tab=DocType.EXHIBITS),
    ]
    return MatterReport(
        metadata=meta,
        counts=counts,
        requested=DocType.EXHIBITS,
        rows=rows,
        files=files_for(rows),
        trace_id="20261023T141300-d4e5f6",
    )


# name -> zero-arg callable producing a Reply. Snapshots live in tests/golden/snapshots/<name>.{txt,html}.
CASES = {
    "docs_happy": lambda: r.render_report(happy()),
    "docs_all_key_documents": lambda: r.render_report(key_docs_all()),
    "docs_single": lambda: r.render_report(single_doc()),
    "docs_more": lambda: r.render_report(more_page()),
    "docs_partial_failures": lambda: r.render_report(partial_with_failures()),
    "docs_all_failed": lambda: r.render_report(all_failed()),
    "docs_assumed_matter": lambda: r.render_outcomes(
        [
            r.Outcome(
                Request(matter="M12205", doc_type=KD, assumed_notes=["I assumed you meant M12205."]),
                report=key_docs_all(),
            )
        ]
    ),
    "summary_only": lambda: r.render_report(summary_only()),
    "summary_no_final_date": lambda: r.render_report(no_final_date()),
    "empty_tab": lambda: r.render_report(empty_tab()),
    "past_end": lambda: r.render_report(past_end()),
    "not_found": lambda: r.render_outcomes([r.Outcome(Request(matter="M99999"), error=MatterNotFound("M99999"))]),
    "site_unavailable": lambda: r.render_outcomes(
        [
            r.Outcome(
                Request(matter="M12205", doc_type=OD),
                error=SiteUnavailable("timed out", trace_id="20261023T141205-a1b2c3"),
            )
        ]
    ),
    "layout_changed_no_trace": lambda: r.render_outcomes(
        [r.Outcome(Request(matter="M12205", doc_type=OD), error=LayoutChanged("counts"))]
    ),
    "empty_tab_error": lambda: r.render_outcomes(
        [r.Outcome(Request(matter="M12205", doc_type=DocType.RECORDINGS), error=EmptyTab("M12205", DocType.RECORDINGS))]
    ),
    "multi_request": lambda: r.render_outcomes(
        [
            r.Outcome(Request(matter="M12205", doc_type=OD), report=happy()),
            r.Outcome(Request(matter="M11111", doc_type=DocType.EXHIBITS), report=m11111()),
            r.Outcome(Request(matter="M99999", doc_type=KD), error=MatterNotFound("M99999")),
        ],
        not_processed=reqs(("M12345", DocType.TRANSCRIPTS)),
    ),
    "clarify_no_matter": lambda: r.render_clarify(Clarify(reason="no_matter", question="Which matter?")),
    "clarify_invalid": lambda: r.render_clarify(
        Clarify(reason="invalid_matter_like", question="M1234 is not a matter number")
    ),
    "clarify_unknown_type": lambda: r.render_clarify(
        Clarify(reason="unknown_type", question="Which type?"), summary_only()
    ),
    "watch_confirm": lambda: r.render_watch_confirm(summary_only()),
    "unwatch": lambda: r.render_unwatch("M12205"),
    "unwatch_not_watching": lambda: r.render_unwatch("M12205", was_watching=False),
    "new_filing": lambda: r.render_new_filing(
        report(counts=COUNTS | {OD: 44}),
        before=dict(COUNTS),
        new_rows=[DocRow(doc_no="102690", title="Board Letter - Closing", date=dt.date(2026, 7, 20), tab=OD)],
    ),
    "ack": lambda: r.render_ack(reqs(("M12205", OD), ("M11111", None))),
    "rate_limited": lambda: r.render_rate_limited(dt.datetime(2026, 10, 23, 14, 30, tzinfo=dt.UTC), per_hour=10),
    "help": lambda: r.render_help(),
}
