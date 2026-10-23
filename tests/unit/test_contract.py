import datetime as dt

import pytest

from uarb_agent.config import load_settings
from uarb_agent.models import (
    DOC_TYPES,
    AgentError,
    DocRow,
    DocType,
    InvalidMatterNumber,
    MatterMetadata,
    MatterReport,
    normalize_matter,
)


def test_doc_types_match_site_labels_in_order():
    assert [str(t) for t in DOC_TYPES] == [
        "Exhibits",
        "Key Documents",
        "Other Documents",
        "Transcripts",
        "Recordings",
    ]


@pytest.mark.parametrize("raw", ["M12205", "m12205", "M-12205", "m 12205", " M12205 "])
def test_normalize_matter(raw):
    assert normalize_matter(raw) == "M12205"


@pytest.mark.parametrize("raw", ["M1234", "M123456", "12205", "X12205", ""])
def test_normalize_matter_rejects(raw):
    with pytest.raises(InvalidMatterNumber) as e:
        normalize_matter(raw)
    assert isinstance(e.value, AgentError)


def test_report_round_trips_json():
    report = MatterReport(
        metadata=MatterMetadata(
            matter_no="M12205", title="t", type="Water", category="c", date_received=dt.date(2025, 4, 7)
        ),
        counts={t: 0 for t in DocType},
        requested=DocType.EXHIBITS,
        rows=[DocRow(doc_no="H-4(C)-i", title="x", date=dt.date(2025, 5, 1), tab=DocType.EXHIBITS)],
        trace_id="abc",
    )
    again = MatterReport.model_validate_json(report.model_dump_json())
    assert again == report
    assert again.downloaded == []


def test_settings_defaults_without_env():
    s = load_settings({})
    assert (s.max_docs, s.zip_budget_mb, s.uarb_concurrency, s.llm_enabled) == (10, 16, 2, False)
    assert not s.llm_active


def test_settings_from_env():
    s = load_settings({"LLM_ENABLED": "true", "ANTHROPIC_API_KEY": "k", "MAX_DOCS": "5", "AGENT_EMAIL": ""})
    assert s.llm_active and s.max_docs == 5 and s.agent_email is None
