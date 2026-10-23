import email
import smtplib
from email import policy
from pathlib import Path

import pytest

from tests.mail_fakes import FakeMailbox, FakeServer, FakeTransport, make_email
from uarb_agent.jobs.ledger import Ledger, Status
from uarb_agent.mail.outbox import Outbox, StaleJob, is_permanent
from uarb_agent.mail.threading import build_reply


@pytest.fixture
def env(tmp_path):
    server = FakeServer(tmp_path / "server")
    transport = FakeTransport(tmp_path / "server")
    ledger = Ledger(tmp_path / "ledger.db")
    outbox = Outbox(ledger, transport, tmp_path / "outbox")
    return server, transport, ledger, outbox


def start_job(server, ledger, mid="<orig-1@example.com>"):
    raw = make_email(message_id=mid)
    uid = server.deliver(raw)
    ledger.record(mid, uid=uid, sender="grader@example.com")
    assert ledger.claim(mid)
    original = email.message_from_bytes(raw, policy=policy.default)
    return build_reply(original, from_addr="agent@gmail.com", text="here you go", trace_id="t1")


def test_deliver_then_settle(env):
    server, transport, ledger, outbox = env
    reply = start_job(server, ledger)
    assert outbox.deliver("<orig-1@example.com>", reply)
    job = ledger.get("<orig-1@example.com>")
    assert job.status == Status.SENT
    assert job.reply_message_id == reply["Message-ID"]
    assert server.sent_ids() == [reply["Message-ID"]]
    assert transport.calls == [("agent@gmail.com", ["grader@example.com"])]
    # the stored .eml is exactly what went over SMTP
    assert server.sent() == [Path(job.eml_path).read_bytes()]

    mailbox = FakeMailbox(server)
    assert outbox.settle(mailbox) == ["<orig-1@example.com>"]
    assert ledger.get("<orig-1@example.com>").status == Status.DONE
    assert mailbox.done_calls == ["1"]
    assert mailbox.fetch_unseen() == []


def test_compose_requires_working(env):
    server, _, ledger, outbox = env
    reply = start_job(server, ledger)
    ledger.transition("<orig-1@example.com>", Status.WORKING, Status.RECEIVED)  # lease lost
    with pytest.raises(StaleJob):
        outbox.compose("<orig-1@example.com>", reply)
    assert list(outbox.dir.glob("*.eml")) == []


def test_send_requires_composed(env):
    _, _, ledger, outbox = env
    with pytest.raises(StaleJob):
        outbox.send("<nope@x>")


def test_transient_smtp_error_leaves_row_composed(env, tmp_path):
    server, _, ledger, outbox = env
    outbox.transport = FakeTransport(tmp_path / "server", fail_with=smtplib.SMTPServerDisconnected("gone"))
    reply = start_job(server, ledger)
    with pytest.raises(smtplib.SMTPServerDisconnected):
        outbox.deliver("<orig-1@example.com>", reply)
    assert ledger.get("<orig-1@example.com>").status == Status.COMPOSED

    outbox.transport = FakeTransport(tmp_path / "server")
    result = outbox.reconcile(FakeMailbox(server))
    assert result.resent == ["<orig-1@example.com>"]
    assert server.sent_ids() == [reply["Message-ID"]]
    assert ledger.get("<orig-1@example.com>").status == Status.DONE


def test_permanent_smtp_error_fails_row(env, tmp_path):
    server, _, ledger, outbox = env
    outbox.transport = FakeTransport(
        tmp_path / "server", fail_with=smtplib.SMTPRecipientsRefused({"grader@example.com": (550, b"no such user")})
    )
    reply = start_job(server, ledger)
    assert not outbox.deliver("<orig-1@example.com>", reply)
    job = ledger.get("<orig-1@example.com>")
    assert job.status == Status.FAILED and "smtp" in job.reason


@pytest.mark.parametrize(
    ("exc", "permanent"),
    [
        (smtplib.SMTPDataError(552, b"message too big"), True),
        (smtplib.SMTPDataError(421, b"try later"), False),
        (smtplib.SMTPServerDisconnected("x"), False),
        (TimeoutError(), False),
        (smtplib.SMTPSenderRefused(550, b"no", "a@b"), True),
    ],
)
def test_is_permanent(exc, permanent):
    assert is_permanent(exc) is permanent


def test_reconcile_requeues_then_exhausts(env, tmp_path):
    server, _, ledger, outbox = env
    clock = [0.0]
    ledger._clock = lambda: clock[0]
    start_job(server, ledger)  # attempt 1, lease 300s
    clock[0] += 301
    assert outbox.reconcile(FakeMailbox(server)).requeued == ["<orig-1@example.com>"]
    for _ in range(2):
        assert ledger.claim("<orig-1@example.com>")
        clock[0] += 301
        result = outbox.reconcile(FakeMailbox(server))
    assert result.exhausted == ["<orig-1@example.com>"]
    job = ledger.get("<orig-1@example.com>")
    assert job.status == Status.WORKING and job.attempts == 3
    # still working, so the caller can compose the apology
    assert outbox.reconcile(FakeMailbox(server)).exhausted == []


def test_smtp_transport_ignores_a_failed_quit_after_acceptance(monkeypatch):
    """sendmail returning means the server took the message; a reset on QUIT must not trigger a resend."""
    from uarb_agent.mail.outbox import SmtpSslTransport

    events = []

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def login(self, user, password):
            events.append("login")

        def sendmail(self, frm, to, raw):
            events.append("sendmail")

        def quit(self):
            raise OSError("connection reset by peer")

        def close(self):
            events.append("close")

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    SmtpSslTransport("smtp.test", 465, "u", "p").send(b"x", "a@b", ["c@d"])
    assert events == ["login", "sendmail", "close"]
