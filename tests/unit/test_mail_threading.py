import email
from email import policy

import pytest

from tests.mail_fakes import make_email
from uarb_agent.mail.threading import (
    MAX_REFERENCES,
    build_reply,
    reply_recipient,
    reply_subject,
    thread_key,
)


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Documents please", "Re: Documents please"),
        ("Re: Documents please", "Re: Documents please"),
        ("RE:Documents", "RE:Documents"),
        ("  re :  spaced  ", "re : spaced"),
        ("", "Re: your request"),
        (None, "Re: your request"),
        ("Fwd: M12205", "Re: Fwd: M12205"),
    ],
)
def test_reply_subject(subject, expected):
    assert reply_subject(subject) == expected


def test_reply_goes_to_reply_to_over_from():
    msg = parse(make_email(headers={"Reply-To": "Team <team@example.com>"}))
    assert reply_recipient(msg) == "team@example.com"
    assert reply_recipient(parse(make_email())) == "grader@example.com"


def test_thread_key_uses_references_root():
    first = parse(make_email(message_id="<root@x>"))
    assert thread_key(first) == "<root@x>"
    follow = parse(make_email(message_id="<c@x>", headers={"References": "<root@x> <b@x>", "In-Reply-To": "<b@x>"}))
    assert thread_key(follow) == "<root@x>"
    only_irt = parse(make_email(message_id="<c@x>", headers={"In-Reply-To": "<b@x>"}))
    assert thread_key(only_irt) == "<b@x>"


def test_build_reply_golden_headers(tmp_path):
    orig = parse(make_email(message_id="<c@x>", subject="M12205 docs", headers={"References": "<a@x> <b@x>"}))
    zip_path = tmp_path / "M12205_Other_Documents.zip"
    zip_path.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    reply = build_reply(
        orig, from_addr="agent@gmail.com", text="hello", html="<p>hello</p>", attachments=[zip_path], trace_id="t-123"
    )
    assert reply["To"] == "grader@example.com"
    assert reply["From"] == "UARB Email Agent <agent@gmail.com>"
    assert reply["Subject"] == "Re: M12205 docs"
    assert reply["In-Reply-To"] == "<c@x>"
    assert reply["References"] == "<a@x> <b@x> <c@x>"
    assert reply["Auto-Submitted"] == "auto-replied"
    assert reply["X-Auto-Response-Suppress"] == "All"
    assert reply["X-Agent-Trace"] == "t-123"
    assert reply["Message-ID"].startswith("<") and reply["Message-ID"].endswith("@gmail.com>")
    assert reply["Date"]

    round_trip = parse(reply.as_bytes(policy=policy.SMTP))
    assert round_trip.get_body(("plain",)).get_content().strip() == "hello"
    assert round_trip.get_body(("html",)).get_content().strip() == "<p>hello</p>"
    [att] = list(round_trip.iter_attachments())
    assert att.get_filename() == "M12205_Other_Documents.zip"
    assert att.get_content_type() == "application/zip"
    assert att.get_content() == zip_path.read_bytes()


def test_message_ids_are_unique():
    orig = parse(make_email())
    ids = {build_reply(orig, from_addr="agent@gmail.com", text="x")["Message-ID"] for _ in range(50)}
    assert len(ids) == 50


def test_references_fall_back_to_in_reply_to_and_are_bounded():
    orig = parse(make_email(message_id="<c@x>", headers={"In-Reply-To": "<b@x>"}))
    assert build_reply(orig, from_addr="a@gmail.com", text="x")["References"] == "<b@x> <c@x>"

    long_refs = " ".join(f"<r{i}@x>" for i in range(40))
    orig = parse(make_email(message_id="<c@x>", headers={"References": long_refs}))
    refs = build_reply(orig, from_addr="a@gmail.com", text="x")["References"].split()
    assert len(refs) == MAX_REFERENCES
    assert refs[0] == "<r0@x>" and refs[-1] == "<c@x>"


def test_no_message_id_on_original_omits_in_reply_to():
    orig = parse(make_email(message_id=None))
    reply = build_reply(orig, from_addr="a@gmail.com", text="x")
    assert "In-Reply-To" not in reply and "References" not in reply
