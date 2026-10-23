from email.message import EmailMessage

import pytest

from uarb_agent.mail import gmail_api
from uarb_agent.mail.gmail_api import SEND_URL, GmailApiError, GmailApiTransport
from uarb_agent.mail.outbox import is_permanent
from uarb_agent.mail.threading import REPLY_ID_HEADER, build_reply


class FakeResponse:
    def __init__(self, status: int, text: str = "{}"):
        self.status_code, self.text = status, text


class FakeSession:
    calls: list = []
    status = 200

    def __init__(self, creds):
        self.creds = creds

    def post(self, url, data, headers, timeout):
        FakeSession.calls.append((url, data, headers))
        return FakeResponse(FakeSession.status, '{"error": "nope"}')


@pytest.fixture
def session(monkeypatch):
    FakeSession.calls, FakeSession.status = [], 200
    monkeypatch.setattr(gmail_api, "AuthorizedSession", FakeSession)
    return FakeSession


def test_sends_the_exact_bytes_as_rfc822(session):
    GmailApiTransport("id", "secret", "refresh").send(b"raw message", "a@b", ["c@d"])
    [(url, data, headers)] = session.calls
    assert url == SEND_URL
    assert data == b"raw message"
    assert headers["Content-Type"] == "message/rfc822"


@pytest.mark.parametrize(("status", "permanent"), [(400, True), (403, True), (429, False), (500, False), (503, False)])
def test_errors_say_whether_a_retry_can_help(session, status, permanent):
    session.status = status
    with pytest.raises(GmailApiError) as exc:
        GmailApiTransport("id", "secret", "refresh").send(b"x", "a@b", ["c@d"])
    assert is_permanent(exc.value) is permanent


def test_replies_carry_a_copy_of_their_message_id():
    original = EmailMessage()
    original["From"], original["Subject"], original["Message-ID"] = "x@y.com", "Hi", "<o@y.com>"
    reply = build_reply(original, from_addr="agent@gmail.com", text="body")
    assert reply[REPLY_ID_HEADER] == reply["Message-ID"]
