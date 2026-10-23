import itertools
import threading

import pytest

from tests.mail_fakes import FakeMailbox, FakeServer, make_email
from uarb_agent.mail import inbox
from uarb_agent.mail.inbox import ImapMailbox, parse_inbound, run_inbox


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(inbox, "backoff_delays", lambda: itertools.repeat(0))


def test_parse_inbound():
    raw = make_email(message_id="<c@x>", headers={"References": "<root@x> <b@x>"})
    item = parse_inbound("7", raw)
    assert (item.uid, item.message_id, item.sender, item.thread_key) == ("7", "<c@x>", "grader@example.com", "<root@x>")
    assert item.subject == "Documents please"


def test_missing_message_id_gets_a_stable_key():
    raw = make_email(message_id=None)
    a, b = parse_inbound("1", raw), parse_inbound("2", raw)
    assert a.message_id == b.message_id
    assert a.message_id.startswith("<no-id-")
    assert a.thread_key == a.message_id


def test_gmail_criteria_skip_done_label():
    box = ImapMailbox("imap.gmail.com", "a@gmail.com", "pw")
    assert box.gmail
    assert str(box._unseen_criteria()) == '(NOT (X-GM-LABELS "uarb-agent/done") UNSEEN)'
    assert str(ImapMailbox("imap.example.com", "a", "pw")._unseen_criteria()) == "(UNSEEN)"


def test_loop_fetches_reconnects_and_falls_back_to_polling(tmp_path):
    server = FakeServer(tmp_path)
    server.deliver(make_email(message_id="<1@x>"))
    stop = threading.Event()
    seen: list[str] = []
    clients: list[FakeMailbox] = []
    attempts = itertools.count()

    def connect():
        n = next(attempts)
        if n == 0:
            raise ConnectionResetError("boom")  # first connect fails; loop must retry
        client = FakeMailbox(server, idle=(n == 1))
        clients.append(client)
        return client

    def on_batch(client, batch):
        for item in batch:
            seen.append(item.message_id)
            client.mark_done(item.uid)
        if len(seen) == 1:
            server.deliver(make_email(message_id="<2@x>"))
            raise OSError("socket dropped mid-batch")  # second connection picks up <2@x> on catch-up
        stop.set()

    healthy = []
    run_inbox(connect, on_batch, stop, poll_interval=0, on_healthy=lambda: healthy.append(1))
    assert seen == ["<1@x>", "<2@x>"]
    assert all(c.closed for c in clients)
    assert len(clients) == 2


def test_loop_rearms_idle_and_polls_without_it(tmp_path):
    server = FakeServer(tmp_path)
    stop = threading.Event()
    client = FakeMailbox(server, idle=False)
    ticks = itertools.count()

    def on_healthy():
        if next(ticks) == 3:
            stop.set()

    run_inbox(lambda: client, lambda c, b: None, stop, idle_timeout=55, poll_interval=0, on_healthy=on_healthy)
    assert client.waits == [55, 55, 55, 55]  # tried IDLE each round, polled when it was unavailable


def test_batch_errors_propagate(tmp_path):
    server = FakeServer(tmp_path)
    server.deliver(make_email())

    def on_batch(client, batch):
        raise KeyError("bug")

    with pytest.raises(KeyError):
        run_inbox(lambda: FakeMailbox(server), on_batch, threading.Event())
