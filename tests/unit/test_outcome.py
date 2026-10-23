"""Outcome extraction from Board Orders / Decisions. Page texts are trimmed from the real documents (pymupdf output)."""

import datetime as dt
from decimal import Decimal

import pymupdf
import pytest

from uarb_agent.extract.outcome import (
    Cited,
    Outcome,
    best_outcome,
    condition_label,
    extract_outcome,
    format_money,
    is_outcome_title,
    parse_outcome,
    render_line,
)

# M12205, Key Document 102674 'Board Order'
ORDER_12205 = [
    """Document: 325280
ORDER
M12205
NOVA SCOTIA REGULATORY AND APPEALS BOARD
IN THE MATTER OF AN APPLICATION by HALIFAX REGIONAL WATER
COMMISSION for approval of the Windsor Street Exchange Redevelopment Project –
Construction for a cost of $64,769,000
BEFORE:
Julia E. Clark, LL.B., Vice Chair
ORDER
Halifax Regional Water Commission (Halifax Water) applied to the Nova Scotia
Regulatory and Appeals Board (Board) for approval of the Windsor Street Exchange
Redevelopment Project – Construction for a cost of $64,769,000.
In its Decision dated October 23, 2025, the Board approved the project with the changes
directed in the Decision, subject to a Compliance Filing due to be filed on November 14,
2025.  Halifax Water provided responses on November 28, 2025, after the Board
approved Halifax Water’s request for an extension.
""",
    """Document: 325280
The Board notes the updated Compliance Filing indicates an overall reduction in the
estimated project cost. The Board is satisfied that the updated Compliance Filing reflects
the Board’s Decision.
The Board approves the application, as amended in the Decision and reflected in the
updated Compliance Filing, for a total project cost of $59,143,000, inclusive of net HST,
and orders that:
Halifax Water is directed to continue to file semi-annual (every six months) reports
including updated project costs, and an updated project timeline, until project completion,
including a breakdown of the magnitude of significant cost changes impacting the five
project components.
DATED at Halifax, Nova Scotia, this 8th day of July 2026.
__________________________
Clerk of the Board
""",
]

# M12205, Key Document 99761 'Board Decision' (cover, table of contents, last pages)
DECISION_12205 = [
    """Document: 325269
DECISION
2025 NSRAB 118
M12205
IN THE MATTER OF AN APPLICATION by HALIFAX REGIONAL WATER
COMMISSION for approval of the Windsor Street Exchange Redevelopment Project –
Construction for a cost of $64,769,000
DECISION DATE:
October 23, 2025
DECISION:
Application approved with the changes directed in this
Decision, subject to a Compliance Filing.
""",
    """- 2 -
Document: 325269
TABLE OF CONTENTS
5.0
FINDINGS .......................................................................................................... 22
6.0
CONCLUSION .................................................................................................... 32
""",
    """- 3 -
Document: 325269
[1]
On April 7, 2025, Halifax Water applied to the Board for approval of the WSE Redevelopment Project, at a total
project cost of $69,275,000, including construction and design costs.
""",
    """- 32 -
Document: 325269
[87]
In addition to the Board’s directives on the compliance filing described
above, the Board directs Halifax Water to file the following:
11. Semi-annual (every six months) project costs updates commencing April 30,
2026, until project completion.
6.0
CONCLUSION
[88]
The Board approves the proposed project in principle, subject to the
compliance filing to be filed by November 14, 2025.
[89]
An Order will issue accordingly.
DATED at Halifax, Nova Scotia, this 23rd day of October 2025.
""",
]

# M12383, Key Document 101689 'Board Order': makes an earlier approval effective; no new verdict
ORDER_12383 = [
    """Document: 329619
ORDER
M12383
IN THE MATTER OF AN APPLICATION by the TOWN OF AMHERST to change its
mutual boundary with the MUNICIPALITY OF THE COUNTY OF CUMBERLAND
In an Amended Decision and Order issued November 26, 2025 (2025 NSRAB 132), the
Board approved the application, subject to ordering a subsequent effective date:
""",
    """- 2 -
Document: 329619
The Board is satisfied that the change in mutual boundary can now be made effective.
The Board orders that:
1. The change in mutual boundary between the Town of Amherst and the Municipality
of the County of Cumberland approved in the Board’s Decision and Order, 2025
NSRAB 132, is effective immediately.
DATED at Halifax, Nova Scotia, this 21st day of April 2026.
""",
]

# M10431, Key Document 89360 'Board Order' (GRA): lettered directives, an item running over two sentences
ORDER_10431 = [
    """ORDER
M10431
The Board orders that:
1. The 2022-2024 GRA Settlement Agreement dated November 24, 2022, attached
hereto as Schedule A, is approved, subject to the Board’s refusal of the AMI opt\xad
out fee.
9. NS Power is directed as follows (with paragraph references to the Board’s
Decision):
a) Submit annual reports on April 1, 2024-2026, summarizing actual storm restoration costs; [para. 332]
b) Include full detail on all storm restoration costs in each Storm
Rider application. Also, NS Power is to engage with stakeholders; [para. 338]
Document: 301609
""",
    """-3-
e) File semi-annual progress reports about the stakeholder engagement
process, starting January 31, 2024; [para. 367]
h) Keep the Annapolis Tidal Generation Facility in property, plant and
equipment; [para. 387]
DATED at Halifax, Nova Scotia, this 27th da^of March 2023.
Clerk of the Board
""",
]


def _order() -> Outcome:
    return parse_outcome(ORDER_12205, doc_no="102674", title="Board Order")


# ------------------------------------------------------------------ fields


def test_order_amounts_date_and_condition_with_pages():
    o = _order()
    assert (o.applied.value, o.applied.page) == (Decimal(64_769_000), 1)
    assert (o.approved.value, o.approved.page) == (Decimal(59_143_000), 2)
    assert (o.date.value, o.date.page) == (dt.date(2026, 7, 8), 2)
    assert o.disposition.value == "approved"
    assert len(o.conditions) == 1
    assert o.conditions[0].page == 2
    assert o.conditions[0].value.startswith("Halifax Water is directed to continue to file semi-annual")
    assert "$59,143,000" in o.approved.quote
    assert o.pages == [1, 2]


def test_order_recital_past_tense_is_not_the_verdict():
    # 'the Board approved the project ... subject to a Compliance Filing' on p. 1 is history, not a condition
    o = _order()
    assert all("Compliance Filing due" not in c.value for c in o.conditions)
    assert o.disposition.page == 2


def test_decision_uses_conclusion_not_findings():
    o = parse_outcome(DECISION_12205, doc_no="99761", title="Board Decision")
    assert (o.applied.value, o.applied.page) == (Decimal(64_769_000), 1)  # the caption, not para [1]'s $69,275,000
    assert o.approved is None  # 'in principle' with no figure: no amount invented
    assert (o.disposition.value, o.disposition.page) == ("approved in principle", 4)
    assert (
        o.summary.value
        == "Application approved with the changes directed in this Decision, subject to a Compliance Filing."
    )
    assert (o.date.value, o.date.page) == (dt.date(2025, 10, 23), 4)
    assert [c.value for c in o.conditions] == [
        "The Board approves the proposed project in principle, subject to the compliance filing to be filed by "
        "November 14, 2025."
    ]


def test_order_without_new_verdict_finds_nothing_to_report():
    o = parse_outcome(ORDER_12383, doc_no="101689", title="Board Order")
    assert o.date.value == dt.date(2026, 4, 21)
    assert (o.applied, o.approved, o.disposition, o.summary, o.conditions) == (None, None, None, None, ())
    assert not o.found
    assert render_line(o) is None


def test_lettered_directives_continuations_and_ocr_date():
    o = parse_outcome(ORDER_10431, doc_no="89360", title="Board Order")
    assert o.date.value == dt.date(2023, 3, 27)  # 'this 27th da^of March 2023'
    values = [c.value for c in o.conditions]
    assert values[0].endswith("subject to the Board’s refusal of the AMI opt-out fee.")
    assert values[1].startswith("Submit annual reports")
    assert values[2].endswith("Also, NS Power is to engage with stakeholders; [para. 338]")  # one item, two sentences
    assert values[3].startswith("File semi-annual progress reports")
    assert values[4].startswith("Keep the Annapolis")
    assert [c.page for c in o.conditions] == [1, 1, 1, 2, 2]
    assert o.approved is None and o.disposition is None


def test_amount_outside_an_approval_sentence_is_not_taken():
    pages = [
        "ORDER The Board approves the application and orders that: 1. The utility is to file a report. "
        "The cost of $5,000,000 is noted. DATED at Halifax, Nova Scotia, this 1st day of May 2024."
    ]
    o = parse_outcome(pages, doc_no="1", title="Board Order")
    assert o.approved is None
    assert o.applied is None  # 'application ... cost of' inside the operative part is not the request


def test_million_and_cents():
    pages = [
        "IN THE MATTER OF AN APPLICATION by X for approval of a project at a total cost of $12.5 million. "
        "The Board approves the application for a total amount of $1,234.56. "
        "DATED at Halifax, Nova Scotia, this 2nd day of June 2024."
    ]
    o = parse_outcome(pages, doc_no="1", title="Board Order")
    assert o.applied.value == Decimal("12500000.0")
    assert o.approved.value == Decimal("1234.56")
    assert format_money(o.applied.value) == "$12,500,000"
    assert format_money(o.approved.value) == "$1,234.56"
    # The reply shows each amount as the document writes it, never a reformatted figure.
    assert render_line(o).startswith("Board decision: approved $1,234.56 of $12.5 million requested (")


def test_amounts_are_never_rounded_or_rates():
    pages = [
        "APPLICATION by X for approval of a project at a total cost of $1,234,567.567. "
        "The Board approves the application for a total amount of $0.125 per kWh. "
        "DATED at Halifax, Nova Scotia, this 2nd day of June 2024."
    ]
    o = parse_outcome(pages, doc_no="1", title="Board Order")
    assert o.approved is None  # a rate, not an amount; and never '$0.12' or '$0'
    assert o.applied.literal == "$1,234,567.567"
    line = render_line(o)
    assert line is not None and "$1,234,567.57" not in line


def test_bad_date_is_dropped_not_guessed():
    o = parse_outcome(
        ["The Board orders that: DATED at Halifax, Nova Scotia, this 31st day of Juny 2024."], doc_no="1", title="Order"
    )
    assert o.date is None


# ------------------------------------------------------------------ choosing documents


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Board Order", True),
        ("Board Decision", True),
        ("Board Amended Decision and Order", True),
        ("Hearing Order", False),
        ("Amended Hearing Order", False),
        ("Notice of Paper Hearing", False),
        ("Board Decision Letter - Scope of IRs", False),
        ("Final Issues List", False),
    ],
)
def test_is_outcome_title(title, expected):
    assert is_outcome_title(title) is expected


def test_best_outcome_prefers_an_approved_amount_then_input_order():
    order = _order()
    decision = parse_outcome(DECISION_12205, doc_no="99761", title="Board Decision")
    empty = parse_outcome(ORDER_12383, doc_no="101689", title="Board Order")
    assert best_outcome([decision, None, order]) is order
    assert best_outcome([empty, decision]) is decision
    assert best_outcome([empty, None]) is None
    twin = parse_outcome(DECISION_12205, doc_no="99762", title="Board Decision")
    assert best_outcome([decision, twin]) is decision


# ------------------------------------------------------------------ rendering


def test_render_line_m12205_order():
    assert render_line(_order()) == (
        "Board decision: approved $59,143,000 of $64,769,000 requested (Board Order 102674, July 8, 2026, pp. 1-2); "
        "conditions: semi-annual (every six months) reports."
    )


def test_render_line_decision_uses_its_own_summary():
    o = parse_outcome(DECISION_12205, doc_no="99761", title="Board Decision")
    assert render_line(o) == (
        "Board decision: application approved with the changes directed in this Decision, "
        "subject to a Compliance Filing "
        "(Board Decision 99761, October 23, 2025, pp. 1, 4); conditions: subject to the compliance filing to be "
        "filed by November 14, 2025."
    )


def test_render_line_conditions_only_puts_reporting_first():
    o = parse_outcome(ORDER_10431, doc_no="89360", title="Board Order")
    assert render_line(o) == (
        "Board decision (Board Order 89360, March 27, 2023, pp. 1-2): conditions: annual reports; "
        "semi-annual progress reports; and 3 more."
    )


def test_render_line_none():
    assert render_line(None) is None


def test_condition_label():
    assert condition_label("X is directed to file quarterly reports on spending.") == "quarterly reports"
    assert condition_label("The plan is approved, subject to the Board's review.") == "subject to the Board's review"
    long = "Engage in a consultative process to develop a Climate Change Adaptation Plan to be filed with the Board"
    assert condition_label(long).endswith("...")


def test_cited_is_immutable():
    c = Cited(value=1, page=1, quote="x")
    with pytest.raises(AttributeError):
        c.page = 2  # type: ignore[misc]


# ------------------------------------------------------------------ files


def _pdf(path, pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 560, 800), text, fontsize=9)
    doc.save(path)
    doc.close()


def test_extract_outcome_from_pdf(tmp_path):
    path = tmp_path / "102674.pdf"
    _pdf(path, ORDER_12205)
    o = extract_outcome(path, doc_no="102674", title="Board Order")
    assert o is not None
    assert (o.approved.value, o.approved.page) == (Decimal(59_143_000), 2)
    assert (o.applied.page, o.date.value) == (1, dt.date(2026, 7, 8))


def test_extract_outcome_none_for_scan_non_pdf_and_garbage(tmp_path):
    scan = tmp_path / "scan.pdf"
    _pdf(scan, ["", ""])
    assert extract_outcome(scan, doc_no="1", title="Board Order") is None
    xlsx = tmp_path / "a.xlsx"
    xlsx.write_bytes(b"PK\x03\x04")
    assert extract_outcome(xlsx, doc_no="1", title="Board Order") is None
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf")
    assert extract_outcome(junk, doc_no="1", title="Board Order") is None
