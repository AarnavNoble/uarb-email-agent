import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from uarb_agent.config import load_settings
from uarb_agent.intent import llm_fallback
from uarb_agent.intent.aliases import find_types, match_type
from uarb_agent.intent.llm_fallback import TOOL, LLMGuess, guess
from uarb_agent.intent.parser import clean_body, parse, strip_quotes
from uarb_agent.models import CommandKind, DocType

ROOT = Path(__file__).resolve().parents[2]


def _eval_module():
    spec = importlib.util.spec_from_file_location("eval_parser", ROOT / "scripts" / "eval_parser.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- eval set ---------------------------------------------------------------------------------


def test_eval_set_is_big_and_varied():
    cases = _eval_module().load()
    tags = {t for c in cases for t in c["tags"]}
    assert len(cases) >= 40
    assert {
        "canonical",
        "typo",
        "mobile",
        "html",
        "multi",
        "no-matter",
        "invalid",
        "follow-up",
        "injection",
        "auto",
    } <= tags


def test_rules_accuracy_on_eval_set():
    ev = _eval_module()
    cases = ev.load()
    correct, misses = ev.evaluate(cases, llm=None)
    assert correct / len(cases) >= 0.95, misses
    canon = [c for c in cases if "canonical" in c["tags"]]
    assert ev.evaluate(canon, llm=None)[0] == len(canon)


# --- cleaning ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Exhibits please\n\nOn Thu, Oct 23, 2025 at 9:14 AM Agent <c@x.com> wrote:\n> M11807 key documents",
        "Exhibits please\n\nOn Thu, Oct 23, 2025 at 9:14 AM UARB Email Agent <\nc@x.com> wrote:\n\nM11807 key docs",
        "Exhibits please\n-----Original Message-----\nFrom: x\nM11807 key documents",
        "Exhibits please\n\nFrom: Agent <c@x.com>\nSent: Thursday\nM11807 key documents",
        "Exhibits please\n> M11807 key documents\n",
        "Exhibits please\n-- \nJane | M11807 key documents team",
    ],
)
def test_strip_quotes_keeps_only_new_text(body):
    assert strip_quotes(body) == "Exhibits please"


def test_html_only_body_is_converted():
    assert "Key Documents for M12205" in clean_body("", "<p>Key <b>Documents</b> for <i>M12205</i></p>")


# --- aliases ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("exibits", DocType.EXHIBITS),
        ("Key-Docs", DocType.KEY_DOCUMENTS),
        ("key documnets", DocType.KEY_DOCUMENTS),
        ("others", DocType.OTHER_DOCUMENTS),
        ("transcript", DocType.TRANSCRIPTS),
        ("the audio", DocType.RECORDINGS),
        ("recrodings", DocType.RECORDINGS),
    ],
)
def test_match_type(text, expected):
    assert match_type(text) == expected


@pytest.mark.parametrize(
    "text", ["according to the docs", "older documents", "an exhibition", "for the record", "the documents", "invoices"]
)
def test_no_false_type(text):
    assert find_types(text) == []


# --- parser behaviours not pinned by the eval set ---------------------------------------------


def test_thread_state_as_object():
    state = SimpleNamespace(matter="M12205", doc_type="Exhibits", next_offset=20, pending=None)
    res = parse("Re: M12205", "more", thread_state=state, llm=None)
    assert res.command.kind is CommandKind.MORE
    assert (res.requests[0].doc_type, res.requests[0].offset) == (DocType.EXHIBITS, 20)


def test_watch_out_is_not_a_command():
    res = parse("", "Watch out, the other docs on M12205 changed", llm=None)
    assert res.command is None and res.requests[0].doc_type is DocType.OTHER_DOCUMENTS


def test_search_naming_only_a_type_and_matter_is_a_document_request():
    res = parse("Search the Other Documents for M12205", "", llm=None)
    assert res.command is None
    assert (res.requests[0].matter, res.requests[0].doc_type) == ("M12205", DocType.OTHER_DOCUMENTS)


@pytest.mark.parametrize("subject", ['SEARCH "" in M12205', "search ???", "SEARCH -- **"])
def test_search_without_words_asks_what_to_search_for(subject):
    res = parse(subject, "", llm=None)
    assert res.command.kind is CommandKind.SEARCH and res.clarify.reason == "no_query"


def test_search_query_is_capped():
    res = parse("SEARCH " + "rate base " * 5000, "", llm=None)
    query = res.command.args[0]
    assert len(query) <= 200 and query.startswith("rate base") and not query.endswith(" ")


def test_bare_number_hint_in_clarify():
    res = parse("", "other docs for 12205", llm=None)
    assert res.clarify.reason == "no_matter" and "M12205" in res.clarify.question


def test_default_llm_is_off_without_settings(monkeypatch):
    monkeypatch.setattr(llm_fallback, "guess", lambda *a, **k: pytest.fail("LLM called while disabled"))
    assert parse("", "send other docs for 12205").clarify.reason == "no_matter"


# --- LLM fallback -----------------------------------------------------------------------------


class FakeClient:
    def __init__(self, tool_input=None, exc=None):
        self.tool_input, self.exc, self.calls = tool_input, exc, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        block = SimpleNamespace(type="tool_use", name=llm_fallback.TOOL_NAME, input=self.tool_input)
        return SimpleNamespace(content=[block])


SETTINGS = load_settings({"LLM_ENABLED": "true", "ANTHROPIC_API_KEY": "test"})


def test_tool_schema_is_strict():
    schema = TOOL["input_schema"]
    assert TOOL["strict"] is True and schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == {"matter", "doc_type"}


def test_guess_happy_path_and_request_shape():
    client = FakeClient({"matter": "m-12205", "doc_type": "Exhibits"})
    g = guess("could you send the exhibit stuff for 12205", client=client, settings=SETTINGS)
    assert g == LLMGuess(matter="M12205", doc_type=DocType.EXHIBITS)
    kw = client.calls[0]
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["tool_choice"] == {"type": "tool", "name": llm_fallback.TOOL_NAME}
    assert "timeout" in kw


def test_guess_rejects_matter_not_in_text():
    client = FakeClient({"matter": "M00000", "doc_type": "Exhibits"})
    assert guess("exhibits for the halifax water case please", client=client, settings=SETTINGS) is None


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(exc=TimeoutError("slow")),
        FakeClient({"matter": "M12205"}),  # missing key still parses: doc_type None
        FakeClient({"matter": 12205, "doc_type": None}),
        FakeClient(None),
    ],
)
def test_guess_never_raises(client):
    g = guess("M12205", client=client, settings=SETTINGS)
    assert g is None or g.matter in (None, "M12205")


def test_guess_drops_unknown_doc_type():
    g = guess("M12205 invoices", client=FakeClient({"matter": "M12205", "doc_type": "Invoices"}), settings=SETTINGS)
    assert g == LLMGuess(matter="M12205", doc_type=None)


def test_guess_disabled_returns_none_without_client():
    assert guess("M12205 exhibits", settings=load_settings({})) is None


def test_parse_uses_llm_only_when_rules_are_stuck():
    calls = []

    def fake(text):
        calls.append(text)
        return LLMGuess(matter="M12205", doc_type=DocType.KEY_DOCUMENTS)

    assert parse("", "Key Documents for M12205", llm=fake).requests[0].doc_type is DocType.KEY_DOCUMENTS
    assert calls == []

    res = parse("", "can you pull the main filings for 12205?", llm=fake)
    assert [(r.matter, r.doc_type) for r in res.requests] == [("M12205", DocType.KEY_DOCUMENTS)]
    assert res.requests[0].assumed_notes


def test_parse_rules_win_over_llm():
    fake = lambda text: LLMGuess(matter="M11807", doc_type=DocType.EXHIBITS)  # noqa: E731
    res = parse("", "what's new on M12205?", llm=fake)
    assert [(r.matter, r.doc_type) for r in res.requests] == [("M12205", None)]


def test_parse_with_rejected_llm_still_clarifies():
    client = FakeClient({"matter": "M00000", "doc_type": "Exhibits"})
    res = parse("", "exhibits for the water case", llm=lambda t: guess(t, client=client, settings=SETTINGS))
    assert res.clarify.reason == "no_matter" and not res.requests


# --- follow-ups found in review ---------------------------------------------------------------

_SUMMARY_SENT = {"matter": "M12205", "doc_type": None, "pending": {"slot": "doc_type", "matter": "M12205"}}
_DOCS_SENT = {"matter": "M12205", "doc_type": "Other Documents", "next_offset": 10, "pending": None}


@pytest.mark.parametrize("body", ["Thanks!", "thank you so much", "ok, great. thanks"])
def test_thanks_after_a_summary_is_silent(body):
    # Regression: this used to answer "I didn't recognise that document type".
    res = parse("Re: Documents request", body, thread_state=_SUMMARY_SENT, llm=None)
    assert res.clarify and res.clarify.reason == "no_request"


@pytest.mark.parametrize("body", ["send the rest", "the remaining ones please", "Thanks, send me the rest."])
def test_rest_means_more(body):
    res = parse("Re: Documents request", body, thread_state=_DOCS_SENT, llm=None)
    assert res.command and res.command.kind is CommandKind.MORE
    assert res.requests[0].offset == 10


def test_unreadable_follow_up_gets_an_answer_not_silence():
    res = parse("Re: Documents request", "can you resend that?", thread_state=_DOCS_SENT, llm=None)
    assert res.clarify and res.clarify.reason not in ("no_request", "auto_reply")
    assert "M12205" in res.clarify.question


def test_bare_five_digits_answer_a_matter_question():
    state = {"pending": {"slot": "matter", "doc_type": "Exhibits"}}
    res = parse("Re: Documents request", "sorry, it's 12205", thread_state=state, llm=None)
    assert [(r.matter, r.doc_type) for r in res.requests] == [("M12205", DocType.EXHIBITS)]
    assert res.requests[0].assumed_notes == ["I assumed you meant M12205."]


def test_type_beside_an_invalid_number_is_not_given_to_another_matter():
    # Regression: "Also M1234 transcripts" became a request for M99999 Transcripts.
    res = parse("", "the first 2 key documents for M12205, and exhibits for M99999? Also M1234 transcripts.", llm=None)
    assert [(r.matter, r.doc_type) for r in res.requests] == [
        ("M12205", DocType.KEY_DOCUMENTS),
        ("M99999", DocType.EXHIBITS),
    ]
    assert any("M1234" in n for n in res.notes)
