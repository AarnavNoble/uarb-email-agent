import datetime as dt
import re

import pytest

from tests.golden.cases import COUNTS, META, OD, files_for, other_rows, report
from uarb_agent.models import DOC_TYPES, DocRow, DocType, FileResult, MatterReport, Request, Skipped
from uarb_agent.reply import render as r

PARAGRAPH = (
    "M12205 is about Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - "
    "$69,275,000. It relates to Capital Expenditure Approvals within the Water category. The matter had an "
    "initial filing on April 7, 2025 and a final filing on October 23, 2025. The matter is closed; outcome: "
    "Directions given. I found 39 documents in total: 13 Exhibits, 5 Key Documents, 21 Other Documents, and no "
    "Transcripts or Recordings. I downloaded 10 out of the 21 Other Documents and am attaching them as a ZIP here."
)


def test_canonical_paragraph_word_for_word():
    counts = COUNTS | {DocType.KEY_DOCUMENTS: 5, OD: 21}
    rows = [x.model_copy(update={"security": "Public"}) for x in other_rows(21)]
    rep = report(counts=counts, requested=OD, rows=rows, files=files_for(rows[:10]))
    assert PARAGRAPH in r.render_report(rep).text


def _first_paragraph(text: str) -> str:
    return text.split("\n\n")[1]


@pytest.mark.parametrize("total", [1, 2, 3, 9, 10, 11, 43])
@pytest.mark.parametrize("failures", [0, 1, 3])
def test_downloaded_count_is_successes_and_counts_stay_in_site_order(total, failures):
    rows = other_rows(total)
    picked = rows[: min(10, total)]
    files = files_for(picked)
    for f in files[:failures]:
        f.ok = False
    successes = sum(f.ok for f in files)
    rep = report(counts=COUNTS | {OD: total}, requested=OD, rows=rows, files=files)
    para = _first_paragraph(r.render_report(rep).text)

    found = re.search(r"I found (\d+) documents in total: (.+?)\. ", para)
    assert found.group(1) == str(sum(rep.counts.values()))
    assert [m.group(2) for m in re.finditer(r"(\d+) ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", found.group(2))] == [
        r.label(rep.counts[t], t) for t in DOC_TYPES if rep.counts[t]
    ]
    assert found.group(2).endswith("and no Transcripts or Recordings")
    if successes == 0:
        assert "no ZIP" in para
    elif total == 1:
        assert "the only Other Document" in para
    elif total == 2 and successes == 2:
        assert "both Other Documents" in para
    elif successes == total:
        assert f"all {total} Other Documents" in para
    else:
        assert f"I downloaded {successes} out of the {total} " in para


def test_singular_labels():
    rep = report(counts=dict.fromkeys(DOC_TYPES, 1), requested=None)
    assert (
        "I found 5 documents in total: 1 Exhibit, 1 Key Document, 1 Other Document, 1 Transcript, and 1 Recording."
        in r.render_report(rep).text
    )


def test_missing_dates():
    no_final = report(metadata=META.model_copy(update={"date_final": None, "status": "Open"}))
    assert "and has no final filing yet." in r.render_report(no_final).text
    closed = report(metadata=META.model_copy(update={"date_final": None}))
    assert "April 7, 2025; no final filing date is recorded. The matter is closed;" in r.render_report(closed).text
    neither = report(metadata=META.model_copy(update={"date_final": None, "date_received": None}))
    text = r.render_report(neither).text
    assert "filing" not in _first_paragraph(text)


def test_more_reply_names_the_window_and_next_batch():
    rows = other_rows(25)
    rep = report(counts=COUNTS | {OD: 25}, requested=OD, offset=20, rows=rows, files=files_for(rows[20:25]))
    text = r.render_report(rep).text
    assert "I downloaded documents 21-25 out of the 25 Other Documents" in text
    assert "Reply MORE" not in text


def test_more_past_end():
    rep = report(requested=OD, offset=43, rows=other_rows())
    assert r.render_report(rep).text.count("That was all 43 Other Documents on M12205") == 1


def test_html_escapes_titles():
    rows = [DocRow(doc_no="1", title="<script>A & B</script>", date=dt.date(2026, 1, 2), tab=OD)]
    rep = report(counts=COUNTS | {OD: 1}, requested=OD, rows=rows, files=files_for(rows))
    html = r.render_report(rep).html
    assert "<script>" not in html
    assert "&lt;script&gt;A &amp; B&lt;/script&gt;" in html


def test_skipped_and_failed_are_listed_with_reasons():
    rows = other_rows(4)
    files = files_for(rows[:2]) + [FileResult(doc_no=rows[2].doc_no, title=rows[2].title, error="x")]
    skipped = [Skipped(doc_no=rows[3].doc_no, title=rows[3].title, reason="confidential on the UARB site")]
    rep = report(counts=COUNTS | {OD: 4}, requested=OD, rows=rows, files=files, skipped=skipped)
    text = r.render_report(rep).text
    assert f"- {rows[2].doc_no} {rows[2].title}: download failed" in text
    assert f"Skipped as confidential (the UARB site only offers a notice in its place):\n- {rows[3].doc_no} " in text
    assert "I downloaded 2 out of the 4 Other Documents" in text
    assert "I skipped 1 Other Document that is confidential on the UARB site, and 1 more couldn't be included" in text


# ---------------------------------------------------------------- matter paragraph


@pytest.mark.parametrize(
    ("counts", "phrase"),
    [
        ({}, "I found no documents on any of its tabs."),
        ({OD: 1}, "I found 1 document in total, an Other Document."),
        ({DocType.EXHIBITS: 4}, "I found 4 documents in total, all of them Exhibits."),
        (
            {DocType.EXHIBITS: 2, DocType.RECORDINGS: 1},
            "I found 3 documents in total: 2 Exhibits, 1 Recording, and no Key Documents, Other Documents, "
            "or Transcripts.",
        ),
        (
            {DocType.EXHIBITS: 2, DocType.KEY_DOCUMENTS: 1, OD: 3, DocType.TRANSCRIPTS: 1},
            "I found 7 documents in total: 2 Exhibits, 1 Key Document, 3 Other Documents, 1 Transcript, "
            "and no Recordings.",
        ),
    ],
)
def test_total_comes_first_and_empty_tabs_read_naturally(counts, phrase):
    rep = report(counts=dict.fromkeys(DOC_TYPES, 0) | counts, requested=None)
    assert phrase in r.render_report(rep).text


@pytest.mark.parametrize(
    ("type_", "category", "phrase"),
    [
        ("Water", "Capital Expenditure Approvals", " It relates to Capital Expenditure Approvals within the Water "),
        ("Assessment", "", " It's an Assessment matter. "),
        ("Planning", "Other", " It's a Planning matter. "),
        ("", "Rates", " It relates to Rates. "),
    ],
)
def test_type_and_category(type_, category, phrase):
    rep = report(metadata=META.model_copy(update={"type": type_, "category": category}))
    assert phrase in r.render_report(rep).text


def test_type_and_category_both_blank_say_nothing():
    rep = report(metadata=META.model_copy(update={"type": " ", "category": "Other"}))
    assert "relates" not in r.render_report(rep).text and "matter." not in r.render_report(rep).text


def test_status_and_outcome():
    def para(**update):
        return _first_paragraph(r.render_report(report(metadata=META.model_copy(update=update))).text)

    assert "The matter is closed; outcome: Directions given." in para()
    assert "The matter is open. I found" in para(status="Open", outcome=None)
    assert "Outcome: Approved. I found" in para(status=None, outcome="Approved.")
    assert "outcome" not in para(status=None, outcome=None).lower()


# ---------------------------------------------------------------- greeting


@pytest.mark.parametrize(
    ("display", "name"),
    [
        ("Jane Doe", "Jane"),
        ('"Doe, Jane"', "Jane"),
        ("Dr. Jane Doe", "Jane"),
        ("jane doe", "Jane"),
        ("Jean-Luc O'Brien", "Jean-Luc"),
        ("=?utf-8?q?Jos=C3=A9_Garc=C3=ADa?=", "José"),
        ("", None),
        (None, None),
        ("jane@example.com", None),
        ("Jane Doe 2", None),
        ("J. Doe", None),
        ("UARB-Team!", None),
    ],
)
def test_first_name(display, name):
    assert r.first_name(display) == name


def test_greet_by_first_name_or_fallback():
    reply = r.render_help()
    assert reply.text.startswith("Hi there,\n") and "<p>Hi there,</p>" in reply.html
    greeted = r.greet(reply, "Jane Doe")
    assert greeted.text.startswith("Hi Jane,\n") and "<p>Hi Jane,</p>" in greeted.html
    assert greeted.text.count("Jane") == 1
    assert r.greet(reply, "info@example.com") == reply


# ---------------------------------------------------------------- fewer than asked for


def _exhibits(securities: list[str]) -> list[DocRow]:
    start = dt.date(2025, 9, 30)
    return [
        DocRow(
            doc_no=f"E-{i + 1}",
            title=f"Exhibit {chr(65 + i)}",
            date=start - dt.timedelta(days=i),
            security=s,
            tab=DocType.EXHIBITS,
        )
        for i, s in enumerate(securities)
    ]


def _restricted_report(securities: list[str]) -> MatterReport:
    rows = _exhibits(securities)
    public = [x for x in rows if x.security == "Public"]
    skipped = [Skipped(doc_no=x.doc_no, title=x.title, reason="not public") for x in rows if x not in public]
    counts = COUNTS | {DocType.EXHIBITS: len(rows)}
    return report(counts=counts, requested=DocType.EXHIBITS, rows=rows, files=files_for(public), skipped=skipped)


def test_restricted_rows_explain_a_short_batch_and_collapse_into_one_list():
    rep = _restricted_report(["Public", "Confidential", "Board only", "Public", "Confidential"])
    text = r.render_report(rep).text
    assert (
        "I downloaded 2 out of the 5 Exhibits and am attaching them as a ZIP here. The other 3 are confidential or "
        "Board Only on the UARB site, so I skipped them." in text
    )
    assert "Skipped as confidential or Board Only (the UARB site only offers a notice in their place):" in text
    assert "- E-2 Exhibit B\n- E-3 Exhibit C\n- E-5 Exhibit E\n" in text
    assert "Not included" not in text and "not public" not in text


def test_notice_detected_after_download_counts_as_confidential():
    rows = _exhibits(["Public", "Public"])
    skipped = [Skipped(doc_no="E-2", title="Exhibit B", reason="the site served a confidentiality notice instead")]
    rep = report(
        counts=COUNTS | {DocType.EXHIBITS: 2},
        requested=DocType.EXHIBITS,
        rows=rows,
        files=files_for(rows[:1]),
        skipped=skipped,
    )
    assert "The other 1 is confidential on the UARB site, so I skipped it." in r.render_report(rep).text


def test_every_row_restricted():
    text = r.render_report(_restricted_report(["Confidential"] * 3)).text
    assert "All 3 Exhibits are confidential on the UARB site, which only offers a notice in their place" in text
    assert "no ZIP this time" in text


def test_time_budget_stop_offers_more():
    rows = _exhibits(["Public"] * 12)
    rep = report(
        counts=COUNTS | {DocType.EXHIBITS: 12}, requested=DocType.EXHIBITS, rows=rows, files=files_for(rows[:2])
    )
    text = r.render_report(rep).text
    assert (
        "I downloaded 2 out of the 12 Exhibits and am attaching them as a ZIP here. Downloading from the UARB" in text
    )
    assert "reply MORE for the next ones." in text
    assert "Reply MORE to get documents 3-12." in text


def test_full_batch_and_single_doc_get_no_explanation():
    rows = other_rows(43)
    public = [x for x in rows if x.security == "Public"][:10]
    skipped = [Skipped(doc_no=rows[5].doc_no, title=rows[5].title, reason="confidential")]
    full = report(requested=OD, rows=rows, files=files_for(public), skipped=skipped)
    assert "I skipped" not in r.render_report(full).text and "The other" not in r.render_report(full).text
    # GET asks for one document; finding it is not a short batch even though more rows follow.
    one = report(requested=OD, offset=3, rows=rows, files=files_for(rows[3:4]))
    reply = r.render_outcomes([r.Outcome(Request(matter="M12205", doc_type=OD, limit=1, offset=3), report=one)])
    assert "taking a while" not in reply.text


# ---------------------------------------------------------------- self-check


def test_numbers_in_normalises():
    assert r.numbers_in("M01234 costs $69,275,000 on Jul 8, 2026 (1.5 MB)") == {"1234", "69275000", "8", "2026", "1.5"}


def test_check_numbers_rejects_strays():
    with pytest.raises(r.ReplyCheckFailed) as exc:
        r.check_numbers("I found 99 Exhibits and 13 Key Documents", {"13"})
    assert exc.value.numbers == {"99"}


def test_allowed_numbers_cover_report_but_not_noise():
    rep = MatterReport(
        metadata=META,
        counts=COUNTS,
        files=[FileResult(doc_no="102674", title="Order", sha256="9" * 64, size=123456, ok=True)],
        trace_id="t-7",
    )
    allowed = r.allowed_numbers(rep)
    assert {"13", "6", "43", "0", "62", "12205", "69275000", "2025", "7", "23", "102674"} <= allowed
    assert "123456" not in allowed  # sizes only appear formatted, and are added by the renderer
    assert "9" * 64 not in allowed


def test_render_refuses_a_template_number_not_in_the_report():
    block = r._block("not_found", set(), matter="M99999", example=r.MATTER_EXAMPLE)
    with pytest.raises(r.ReplyCheckFailed):
        r._render([block])


def test_render_refuses_inconsistent_report_numbers(monkeypatch):
    real = r._about

    def tampered(report):
        p = real(report)
        p["counts"] = p["counts"].replace("43", "44")
        return p

    monkeypatch.setattr(r, "_about", tampered)
    with pytest.raises(r.ReplyCheckFailed, match="44"):
        r.render_report(report(requested=None))


def test_new_filing_has_subject():
    reply = r.render_new_filing(report(counts=COUNTS | {OD: 44}), before=dict(COUNTS))
    assert reply.subject == "New filing on M12205"
    assert "Other Documents went from 43 to 44" in reply.text


def test_outcome_needs_report_or_error():
    with pytest.raises(ValueError):
        r.render_outcomes([r.Outcome(Request(matter="M12205"))])
