import datetime as dt
import re

import pytest

from tests.golden.cases import COUNTS, META, OD, files_for, other_rows, report
from uarb_agent.models import DOC_TYPES, DocRow, DocType, FileResult, MatterReport, Skipped
from uarb_agent.reply import render as r

PARAGRAPH = (
    "M12205 is about Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - "
    "$69,275,000. It relates to Water within the Capital Expenditure Approvals category. The matter had an "
    "initial filing on April 7, 2025 and a final filing on October 23, 2025. I found 13 Exhibits, 5 Key Documents, "
    "21 Other Documents, 0 Transcripts, and 0 Recordings. I downloaded 10 out of the 21 Other Documents and "
    "attached them as a ZIP."
)


def test_canonical_paragraph_word_for_word():
    counts = COUNTS | {DocType.KEY_DOCUMENTS: 5, OD: 21}
    rows = other_rows(21)
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

    found = re.search(r"I found (.+?)\. ", para).group(1)
    assert [m.group(2) for m in re.finditer(r"(\d+) ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", found)] == [
        r.label(rep.counts[t], t) for t in DOC_TYPES
    ]
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
        "I found 1 Exhibit, 1 Key Document, 1 Other Document, 1 Transcript, and 1 Recording."
        in r.render_report(rep).text
    )


def test_missing_dates():
    no_final = report(metadata=META.model_copy(update={"date_final": None}))
    assert "and has no final filing yet." in r.render_report(no_final).text
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
    rows = other_rows(5)
    files = files_for(rows[:2]) + [FileResult(doc_no=rows[2].doc_no, title=rows[2].title, error="x")]
    skipped = [Skipped(doc_no=rows[3].doc_no, title=rows[3].title, reason="confidential on the UARB site")]
    rep = report(counts=COUNTS | {OD: 5}, requested=OD, rows=rows, files=files, skipped=skipped)
    text = r.render_report(rep).text
    assert f"- {rows[2].doc_no} {rows[2].title}: download failed" in text
    assert f"- {rows[3].doc_no} {rows[3].title}: confidential on the UARB site" in text
    assert "I downloaded 2 out of the 5 Other Documents" in text


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
    assert {"13", "6", "43", "0", "12205", "69275000", "2025", "7", "23", "102674"} <= allowed
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
    from uarb_agent.models import Request

    with pytest.raises(ValueError):
        r.render_outcomes([r.Outcome(Request(matter="M12205"))])
