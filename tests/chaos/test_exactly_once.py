"""kill -9 the agent at every step of a job, restart it, and check the grader gets exactly one reply.

A child process runs one job against the fake mail server and SIGKILLs itself at the chosen point;
the test then plays the restarted service (reconcile, catch-up fetch, settle) and counts replies in Sent.
"""

from __future__ import annotations

import email
import os
import signal
import subprocess
import sys
import textwrap
from email import policy
from pathlib import Path

import pytest

from tests.mail_fakes import FakeMailbox, FakeServer, FakeTransport, make_email
from uarb_agent.jobs.ledger import Ledger, Status
from uarb_agent.mail.inbox import Inbound
from uarb_agent.mail.outbox import Outbox
from uarb_agent.mail.threading import build_reply

REPO = Path(__file__).resolve().parents[2]
MID = "<orig-1@example.com>"
AGENT = "agent@gmail.com"

CHILD = textwrap.dedent(
    """
    import email, os, signal, sys
    from email import policy
    from pathlib import Path
    from uarb_agent.jobs.ledger import Ledger
    from uarb_agent.mail.outbox import Outbox
    from uarb_agent.mail.threading import build_reply
    from tests.mail_fakes import FakeTransport

    root, point, uid, mid = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]

    def die_at(p):
        if p == point:
            os.kill(os.getpid(), signal.SIGKILL)

    class KillingTransport(FakeTransport):
        def send(self, *args):
            super().send(*args)
            die_at("after_smtp")  # SMTP accepted it, ledger never heard

    ledger = Ledger(root / "ledger.db")
    outbox = Outbox(ledger, KillingTransport(root / "server"), root / "outbox")
    raw = (root / "inbound.eml").read_bytes()
    ledger.record(mid, uid=uid, sender="grader@example.com")
    ledger.claim(mid)
    die_at("before_compose")
    reply = build_reply(email.message_from_bytes(raw, policy=policy.default), from_addr="agent@gmail.com", text="hi")
    outbox.compose(mid, reply)
    die_at("after_compose")
    outbox.send(mid)
    die_at("after_send")
    """
)


def handle(ledger: Ledger, outbox: Outbox, item: Inbound) -> None:
    """Minimal stand-in for the worker: one reply per new or requeued message."""
    ledger.record(item.message_id, uid=item.uid, sender=item.sender, thread_key=item.thread_key)
    if not ledger.claim(item.message_id):
        return  # duplicate delivery, or already past received
    reply = build_reply(item.msg, from_addr=AGENT, text="hi")
    outbox.deliver(item.message_id, reply)


def restart(root: Path, server: FakeServer) -> Ledger:
    ledger = Ledger(root / "ledger.db")
    outbox = Outbox(ledger, FakeTransport(root / "server"), root / "outbox")
    mailbox = FakeMailbox(server)
    outbox.reconcile(mailbox, reclaim_all=True)
    for item in mailbox.fetch_unseen():
        handle(ledger, outbox, item)
    outbox.settle(mailbox)
    return ledger


def replies_to(server: FakeServer, mid: str) -> list[bytes]:
    return [r for r in server.sent() if email.message_from_bytes(r, policy=policy.default)["In-Reply-To"] == mid]


@pytest.fixture
def world(tmp_path):
    server = FakeServer(tmp_path / "server")
    raw = make_email(message_id=MID)
    (tmp_path / "inbound.eml").write_bytes(raw)
    uid = server.deliver(raw)
    return tmp_path, server, uid


def run_child(root: Path, point: str, uid: str) -> int:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run([sys.executable, "-c", CHILD, str(root), point, uid, MID], cwd=REPO, env=env, timeout=60)
    return proc.returncode


@pytest.mark.parametrize("point", ["before_compose", "after_compose", "after_smtp", "after_send", "never"])
def test_kill_9_at_each_step_gives_exactly_one_reply(world, point):
    root, server, uid = world
    code = run_child(root, point, uid)
    assert code == (0 if point == "never" else -signal.SIGKILL)

    ledger = restart(root, server)
    assert len(replies_to(server, MID)) == 1
    job = ledger.get(MID)
    assert job.status == Status.DONE
    assert job.reply_message_id == server.sent_ids()[0]
    assert uid in server.seen and "uarb-agent/done" in server.labels[uid]

    # a second restart (or a crash during the first one) changes nothing
    restart(root, server)
    assert len(replies_to(server, MID)) == 1


def test_crash_between_compose_and_send_resends_the_same_bytes(world):
    root, server, uid = world
    assert run_child(root, "after_compose", uid) == -signal.SIGKILL
    composed = Ledger(root / "ledger.db").get(MID)
    assert composed.status == Status.COMPOSED and server.sent() == []
    saved = Path(composed.eml_path).read_bytes()

    restart(root, server)
    assert server.sent() == [saved]  # same Message-ID, so the thread and reconcile both still line up


def test_lost_database_does_not_resend(world):
    root, server, uid = world
    run_child(root, "never", uid)
    restart(root, server)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{root / 'ledger.db'}{suffix}").unlink(missing_ok=True)

    restart(root, server)  # inbox is \\Seen and labelled, so nothing is fetched
    assert len(replies_to(server, MID)) == 1


def test_duplicate_delivery_gets_one_reply(world):
    root, server, _ = world
    server.deliver(make_email(message_id=MID))  # same Message-ID delivered twice (e.g. To + Cc)
    restart(root, server)
    assert len(replies_to(server, MID)) == 1
