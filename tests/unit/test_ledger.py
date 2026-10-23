import threading

import pytest

from uarb_agent.jobs.ledger import IllegalTransition, Ledger, Status, ThreadState


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(tmp_path, clock):
    led = Ledger(tmp_path / "ledger.db", clock=clock)
    yield led
    led.close()


def test_wal_mode(ledger):
    assert ledger._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_record_is_idempotent(ledger):
    assert ledger.record("<a@x>", uid="1", sender="Grader@Example.com", subject="hi", thread_key="<root@x>")
    assert not ledger.record("<a@x>", uid="9", sender="other@x", subject="dup")
    job = ledger.get("<a@x>")
    assert (job.status, job.uid, job.sender, job.thread_key) == (Status.RECEIVED, "1", "grader@example.com", "<root@x>")


def test_thread_key_defaults_to_message_id(ledger):
    ledger.record("<a@x>")
    assert ledger.get("<a@x>").thread_key == "<a@x>"


def test_happy_path_transitions(ledger):
    ledger.record("<a@x>")
    assert ledger.claim("<a@x>")
    assert ledger.transition("<a@x>", Status.WORKING, Status.COMPOSED, reply_message_id="<r@x>", eml_path="/p")
    assert ledger.transition("<a@x>", Status.COMPOSED, Status.SENT)
    assert ledger.transition("<a@x>", Status.SENT, Status.DONE)
    job = ledger.get("<a@x>")
    assert (job.status, job.reply_message_id, job.attempts) == (Status.DONE, "<r@x>", 1)


def test_transition_is_compare_and_set(ledger):
    ledger.record("<a@x>")
    assert ledger.claim("<a@x>")
    assert not ledger.claim("<a@x>")  # second worker loses
    assert not ledger.transition("<a@x>", Status.COMPOSED, Status.SENT)  # not composed yet
    assert ledger.get("<a@x>").status == Status.WORKING


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (Status.RECEIVED, Status.SENT),
        (Status.WORKING, Status.SENT),
        (Status.DONE, Status.RECEIVED),
        (Status.IGNORED, Status.WORKING),
        (Status.FAILED, Status.COMPOSED),
    ],
)
def test_illegal_transitions_raise(ledger, frm, to):
    with pytest.raises(IllegalTransition):
        ledger.transition("<a@x>", frm, to)


def test_transition_rejects_unknown_fields(ledger):
    with pytest.raises(ValueError):
        ledger.transition("<a@x>", Status.RECEIVED, Status.IGNORED, status="done")


def test_ignore_records_reason(ledger):
    ledger.record("<a@x>")
    assert ledger.transition("<a@x>", Status.RECEIVED, Status.IGNORED, reason="auto_submitted")
    assert ledger.get("<a@x>").reason == "auto_submitted"


def test_leases_expire(ledger, clock):
    ledger.record("<a@x>")
    ledger.claim("<a@x>", lease_s=300)
    assert ledger.expired_leases() == []
    clock.t += 200
    assert ledger.renew("<a@x>", lease_s=300)
    clock.t += 299
    assert ledger.expired_leases() == []
    clock.t += 2
    assert [j.message_id for j in ledger.expired_leases()] == ["<a@x>"]


def test_concurrent_claims_have_one_winner(tmp_path):
    path = tmp_path / "ledger.db"
    Ledger(path).record("<a@x>")
    ledgers = [Ledger(path) for _ in range(8)]
    wins = []
    barrier = threading.Barrier(len(ledgers))

    def go(led):
        barrier.wait()
        wins.append(led.claim("<a@x>"))

    threads = [threading.Thread(target=go, args=(led,)) for led in ledgers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


def test_reply_counters(ledger, clock):
    for i in range(3):
        ledger.record(f"<{i}@x>", sender="a@x", thread_key="<t@x>")
        ledger.claim(f"<{i}@x>")
    ledger.record("<ign@x>", sender="a@x", thread_key="<t@x>")
    ledger.transition("<ign@x>", Status.RECEIVED, Status.IGNORED)
    assert ledger.replies_to_sender_since("A@X", clock.t - 3600) == 3
    assert ledger.replies_in_thread("<t@x>") == 3
    clock.t += 3601
    assert ledger.replies_to_sender_since("a@x", clock.t - 3600) == 0
    assert ledger.replies_in_thread("<t@x>") == 3  # the thread cap never expires


def test_thread_state_round_trip(ledger):
    assert ledger.get_thread("<t@x>") is None
    state = ThreadState(
        thread_key="<t@x>",
        matter="M12205",
        doc_type="Other Documents",
        next_offset=10,
        sent_doc_nos=["1", "2"],
        pending={"slot": "doc_type", "matter": "M12205"},
    )
    ledger.save_thread(state)
    got = ledger.get_thread("<t@x>")
    assert got.model_dump(exclude={"updated_at"}) == state.model_dump(exclude={"updated_at"})
    ledger.save_thread(got.model_copy(update={"pending": None, "next_offset": 20}))
    got = ledger.get_thread("<t@x>")
    assert (got.pending, got.next_offset) == (None, 20)


def test_watches(ledger):
    assert ledger.add_watch("A@x", "M12205", {"Exhibits": 13})
    assert not ledger.add_watch("a@x", "M12205", {"Exhibits": 14})
    ledger.add_watch("b@x", "M12205", {})
    assert [w.snapshot for w in ledger.list_watches("a@x")] == [{"Exhibits": 14}]
    assert len(ledger.list_watches()) == 2
    ledger.update_watch("a@x", "M12205", {"Exhibits": 15})
    assert ledger.list_watches("a@x")[0].snapshot == {"Exhibits": 15}
    assert ledger.remove_watch("a@x", "M12205")
    assert not ledger.remove_watch("a@x", "M12205")
    assert [w.sender for w in ledger.list_watches()] == ["b@x"]
