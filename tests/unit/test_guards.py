import email
from email import policy

import pytest

from tests.mail_fakes import make_email
from uarb_agent.jobs.ledger import Ledger
from uarb_agent.mail.guards import loop_reason, rate_reason, screen
from uarb_agent.mail.threading import build_reply

OWN = ["agent@gmail.com"]


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


def bounce() -> bytes:
    """Shape of a Gmail delivery-status notification."""
    return (
        b"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\n"
        b"To: agent@gmail.com\r\n"
        b"Subject: Delivery Status Notification (Failure)\r\n"
        b"Message-ID: <bounce@x>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/report; report-type=delivery-status; boundary="b1"\r\n\r\n'
        b"--b1\r\nContent-Type: text/plain\r\n\r\nAddress not found\r\n"
        b"--b1\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; googlemail.com\r\n"
        b"--b1--\r\n"
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"headers": {"Auto-Submitted": "auto-replied"}}, "auto_submitted"),
        ({"headers": {"Auto-Submitted": "auto-generated; owner-email=x@y"}}, "auto_submitted"),
        ({"headers": {"Precedence": "bulk"}}, "precedence"),
        ({"headers": {"Precedence": "Junk"}}, "precedence"),
        ({"headers": {"Precedence": "list"}}, "precedence"),
        ({"headers": {"X-Autoreply": "yes"}}, "autoreply_header"),
        ({"headers": {"X-Autorespond": "Out of office"}}, "autoreply_header"),
        ({"headers": {"X-Failed-Recipients": "a@b.com"}}, "autoreply_header"),
        ({"headers": {"List-Id": "<news.example.com>"}}, "mailing_list"),
        ({"headers": {"List-Unsubscribe": "<mailto:u@x>"}}, "mailing_list"),
        ({"headers": {"Return-Path": "<>"}}, "null_return_path"),
        ({"sender": "MAILER-DAEMON@mx.example.com"}, "automated_sender"),
        ({"sender": "postmaster@example.com"}, "automated_sender"),
        ({"sender": "noreply@example.com"}, "automated_sender"),
        ({"sender": "No-Reply <no-reply@example.com>"}, "automated_sender"),
        ({"sender": "do-not-reply@example.com"}, "automated_sender"),
        ({"sender": "UARB Email Agent <Agent@Gmail.com>"}, "own_address"),
        ({"sender": "undisclosed-recipients:;"}, "no_sender"),
    ],
)
def test_ignored(kwargs, reason):
    assert loop_reason(parse(make_email(**kwargs)), OWN) == reason


def test_bounce_is_ignored():
    # The sender check fires first for this one; strip it to prove the report check also works.
    assert loop_reason(parse(bounce()), OWN) == "automated_sender"
    raw = bounce().replace(b"mailer-daemon@googlemail.com", b"alerts@example.com")
    assert loop_reason(parse(raw), OWN) == "delivery_report"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"headers": {"Auto-Submitted": "no"}},
        {"headers": {"Precedence": "first-class"}},
        {"headers": {"Return-Path": "<grader@example.com>"}},
        {"headers": {"X-Auto-Response-Suppress": "All"}},  # Outlook sets this on ordinary mail
        {"sender": "noreen@example.com"},
        {"sender": "replyguy@example.com"},
    ],
)
def test_ordinary_mail_passes(kwargs):
    assert loop_reason(parse(make_email(**kwargs)), OWN) is None


def test_our_own_reply_looping_back_is_ignored():
    reply = build_reply(parse(make_email()), from_addr="other-agent@gmail.com", text="x")
    assert loop_reason(parse(reply.as_bytes()), OWN) == "auto_submitted"


def test_canary_token_bypasses_own_address_only_when_it_matches():
    own = {"sender": "agent@gmail.com"}
    good = parse(make_email(**own, headers={"X-Agent-Canary": "s3cret"}))
    bad = parse(make_email(**own, headers={"X-Agent-Canary": "guess"}))
    assert loop_reason(good, OWN, canary_token="s3cret") is None
    assert loop_reason(bad, OWN, canary_token="s3cret") == "own_address"
    assert loop_reason(good, OWN) == "own_address"


class Clock:
    t = 50_000.0

    def __call__(self):
        return self.t


def test_rate_limits(tmp_path):
    clock = Clock()
    led = Ledger(tmp_path / "l.db", clock=clock)
    for i in range(10):
        led.record(f"<{i}@x>", sender="grader@example.com", thread_key=f"<t{i}@x>")
        led.claim(f"<{i}@x>")
    assert rate_reason(led, "Grader@Example.com", "<new@x>") == "sender_rate_limited"
    assert rate_reason(led, "someone@else.com", "<new@x>") is None
    clock.t += 3601
    assert rate_reason(led, "grader@example.com", "<new@x>") is None

    for i in range(20):
        led.record(f"<th{i}@x>", sender=f"s{i}@x", thread_key="<busy@x>")
        led.claim(f"<th{i}@x>")
    assert rate_reason(led, "fresh@x", "<busy@x>") == "thread_cap"


def test_screen_combines_both(tmp_path):
    led = Ledger(tmp_path / "l.db")
    msg = parse(make_email())
    assert screen(msg, led, OWN, thread_key="<orig-1@example.com>") is None
    auto = parse(make_email(headers={"Auto-Submitted": "auto-replied"}))
    assert screen(auto, led, OWN, thread_key="<orig-1@example.com>") == "auto_submitted"


def test_canary_mail_from_ourselves_is_answered_and_not_rate_limited():
    from email.message import EmailMessage

    from uarb_agent.mail.guards import CANARY_HEADER, screen

    class Busy:
        def now(self):
            return 0.0

        def replies_to_sender_since(self, sender, since):
            return 999

        def replies_in_thread(self, thread_key):
            return 0

    msg = EmailMessage()
    msg["From"] = "agent@gmail.com"
    msg[CANARY_HEADER] = "s3cret"
    assert screen(msg, Busy(), ["agent@gmail.com"], thread_key="<t>", canary_token="s3cret") is None
    msg.replace_header(CANARY_HEADER, "wrong")
    assert screen(msg, Busy(), ["agent@gmail.com"], thread_key="<t>", canary_token="s3cret") == "own_address"
