"""Parsers against geometry captured live from M12205 (tests/fixtures/uarb) and the recon HTML."""

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from uarb_agent.models import DOC_TYPES, DocType, LayoutChanged
from uarb_agent.uarb.parse import parse_counts, parse_found_count, parse_metadata, parse_row

FIX = Path(__file__).parents[1] / "fixtures"
UARB = FIX / "uarb"


def load(name: str):
    return json.loads((UARB / name).read_text())


def rows_from_snapshots(name: str, tab: DocType):
    """What the connector does while scrolling: parse every rendered row, keep the first sighting."""
    rows = {}
    for snap in load(name):
        for r in snap["rows"]:
            if row := parse_row(r["boxes"], tab):
                rows.setdefault(row.doc_no, row)
    return list(rows.values())


# --- counts -----------------------------------------------------------------------------------------


def test_counts_from_live_buttons_in_site_order():
    counts = parse_counts(load("buttons_overview.json"))
    assert list(counts) == list(DOC_TYPES)
    assert counts == {
        DocType.EXHIBITS: 13,
        DocType.KEY_DOCUMENTS: 6,
        DocType.OTHER_DOCUMENTS: 43,
        DocType.TRANSCRIPTS: 0,
        DocType.RECORDINGS: 0,
    }


def test_counts_from_recon_html_buttons():
    html = (FIX / "matter.html").read_text()
    captions = re.findall(r">([^<>]+ - \d+)<", html)
    assert parse_counts(captions)[DocType.OTHER_DOCUMENTS] == 43


def test_counts_tolerate_whitespace_and_ignore_other_buttons():
    texts = ["Search", "Hearings", " Exhibits -  2 ", "Key Documents - 0", "Other Documents-7", "Transcripts - 1"]
    texts.append("Recordings - 3")
    assert list(parse_counts(texts).values()) == [2, 0, 7, 1, 3]


def test_counts_missing_tab_is_layout_change():
    with pytest.raises(LayoutChanged, match="Recordings"):
        parse_counts(["Exhibits - 1", "Key Documents - 1", "Other Documents - 1", "Transcripts - 0"])


def test_found_count():
    assert parse_found_count((FIX / "other.html").read_text()) == 43
    assert parse_found_count("Doc No\nSecurity\nTitle\n") is None


# --- metadata ---------------------------------------------------------------------------------------

EXPECTED_META = dict(
    matter_no="M12205",
    title="Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000",
    type="Water",
    category="Capital Expenditure Approvals",
    status="Closed",
    outcome="Directions given",
    date_received=dt.date(2025, 4, 7),
    date_final=dt.date(2025, 10, 23),
)


@pytest.mark.parametrize("fixture", ["header_overview.json", "header_other.json", "header_exhibits.json"])
def test_metadata_by_label_on_each_layout(fixture):
    # the overview says 'Decision Date', tab layouts say 'Date Final Submission(s)'
    assert parse_metadata(load(fixture)).model_dump() == EXPECTED_META


def test_metadata_labels_win_over_position():
    """Rearranged columns (Outcome moved left, Type/Category moved right): position would mislabel, labels do not."""
    boxes = load("header_overview.json")
    by_text = {b["t"]: b for b in boxes}
    by_text["Outcome"]["x"], by_text["Directions given"]["x"] = 700, 690
    by_text["Type\nCategory"]["x"] = 1072
    by_text["Water"]["x"] = by_text["Capital Expenditure Approvals"]["x"] = 1060
    meta = parse_metadata(boxes)
    assert (meta.type, meta.category, meta.outcome) == ("Water", "Capital Expenditure Approvals", "Directions given")


def test_metadata_stacked_labels_as_separate_elements():
    boxes = [b for b in load("header_other.json") if b["t"] != "Matter No\nStatus"]
    boxes += [
        {"t": "Matter No", "x": 12, "y": 94, "w": 60, "h": 14},
        {"t": "Status", "x": 12, "y": 108, "w": 40, "h": 14},
    ]
    meta = parse_metadata(boxes)
    assert (meta.matter_no, meta.status) == ("M12205", "Closed")


def test_metadata_positional_fallback_without_labels():
    values = [b for b in load("header_overview.json") if "\n" not in b["t"] and b["y"] > 150]
    assert parse_metadata(values).model_dump() == EXPECTED_META


def test_metadata_blank_final_date_is_none():
    boxes = [b for b in load("header_other.json") if b["t"] != "10/23/2025"]
    assert parse_metadata(boxes).date_final is None


def test_metadata_bad_date_is_layout_change():
    boxes = load("header_other.json")
    next(b for b in boxes if b["t"] == "04/07/2025")["t"] = "2025-04-07"
    with pytest.raises(LayoutChanged, match="date"):
        parse_metadata(boxes)


def test_metadata_without_matter_number_is_layout_change():
    with pytest.raises(LayoutChanged):
        parse_metadata([b for b in load("header_other.json") if b["t"] != "M12205"])


# --- rows -------------------------------------------------------------------------------------------


def test_other_documents_rows_from_scrolled_snapshots():
    rows = rows_from_snapshots("grid_other.json", DocType.OTHER_DOCUMENTS)
    assert len(rows) == 43 == len({r.doc_no for r in rows})
    recon = [r["fields"]["201"] for r in json.loads((FIX / "rows_other.json").read_text())]
    assert [r.doc_no for r in rows[: len(recon)]] == recon  # site order survives scrolling
    first = rows[0]
    assert (first.doc_no, first.title, first.date, first.ext, first.security, first.tab) == (
        "102674",
        "Board Order",
        dt.date(2026, 7, 8),
        ".pdf",
        "Public",
        DocType.OTHER_DOCUMENTS,
    )
    assert {r.security for r in rows} == {"Public", "Confidential"}
    assert all(r.date and r.ext and r.title for r in rows)


def test_exhibit_numbers_survive():
    rows = rows_from_snapshots("grid_exhibits.json", DocType.EXHIBITS)
    assert len(rows) == 13
    exhibit = next(r for r in rows if r.doc_no == "H-4(C)-i")
    assert exhibit.security == "Confidential"
    assert "CONFIDENTIAL" in exhibit.title


def test_row_ignores_button_captions_and_empty_rows():
    boxes = [
        {"t": "97291", "x": 5, "y": 4, "w": 110, "h": 26},
        {"t": "Hearing Order", "x": 140, "y": 4, "w": 900, "h": 26},
        {"t": ".PDF", "x": 1180, "y": 4, "w": 60, "h": 26},
        {"t": "04/11/2025", "x": 1270, "y": 4, "w": 100, "h": 26},
        {"t": "GO GET IT", "x": 1170, "y": 36, "w": 100, "h": 26},
        {"t": "Public", "x": 5, "y": 36, "w": 110, "h": 26},
    ]
    row = parse_row(boxes, DocType.KEY_DOCUMENTS)
    assert (row.doc_no, row.title, row.ext, row.security) == ("97291", "Hearing Order", ".pdf", "Public")
    assert parse_row([], DocType.KEY_DOCUMENTS) is None
