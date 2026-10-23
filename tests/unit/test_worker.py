import asyncio
import email
import zipfile
from email import policy
from email.message import EmailMessage

import pytest

from tests.fake_site import FakeSite
from tests.mail_fakes import FakeMailbox, FakeServer, FakeTransport, make_email
from uarb_agent.cache.store import Store
from uarb_agent.config import Settings
from uarb_agent.core import Fetcher
from uarb_agent.jobs.ledger import Ledger, Status, ThreadState
from uarb_agent.jobs.worker import EMAIL_TIMEOUT_S, Pipeline, SettlingMailbox, Worker, next_offset
from uarb_agent.mail.inbox import parse_inbound
from uarb_agent.mail.outbox import MAX_ATTEMPTS, Outbox
from uarb_agent.models import DocType

AGENT = "agent@uarb-agent.test"
CANONICAL = "Hi Agent, can you give me a list of Other Documents files from M12205?"


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    site = FakeSite()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    fetcher = Fetcher(settings, connector=site, store=Store(settings.cache_dir))
    pipeline = Pipeline(fetcher, ledger, settings)
    return settings, site, ledger, pipeline


def msg(body: str, subject: str = "Documents", **kw) -> EmailMessage:
    raw = make_email(body=body, subject=subject, **kw)
    return email.message_from_bytes(raw, policy=policy.default)


async def handle(pipeline, m, thread=None, **kw):
    key = str(m["Message-ID"])
    return await pipeline.handle(m, thread_key=key, thread=thread, **kw)


async def test_canonical_request(env):
    _, site, _, pipeline = env
    h = await handle(pipeline, msg(CANONICAL))
    assert "I downloaded 10 out of the 14 Other Documents" in h.reply.text
    assert len(h.attachments) == 1
    with zipfile.ZipFile(h.attachments[0]) as zf:
        assert len([n for n in zf.namelist() if n.endswith(".pdf")]) == 10
    assert h.thread.matter == "M12205" and h.thread.doc_type == "Other Documents"
    assert h.thread.next_offset == 10 and len(h.thread.sent_doc_nos) == 10


async def test_more_continues_from_thread_state(env):
    _, site, _, pipeline = env
    first = await handle(pipeline, msg(CANONICAL))
    more = await handle(pipeline, msg("MORE", subject="Re: Documents"), thread=first.thread)
    assert "documents 11-14 out of the 14" in more.reply.text
    assert more.thread.next_offset == 14
    assert len(set(more.thread.sent_doc_nos)) == 14  # no overlap between the two batches


async def test_summary_sets_pending_slot_and_one_word_reply_fills_it(env):
    _, _, _, pipeline = env
    summary = await handle(pipeline, msg("what's going on with m12205"))
    assert summary.attachments == []
    assert summary.thread.pending == {"slot": "doc_type", "matter": "M12205"}
    follow = await handle(pipeline, msg("Exhibits", subject="Re: Documents"), thread=summary.thread)
    assert "I downloaded all 3 Exhibits" in follow.reply.text


async def test_clarify_and_help_and_silence(env):
    _, site, _, pipeline = env
    h = await handle(pipeline, msg("can I get the exhibits please"))
    assert "M01234" in h.reply.text and h.attachments == []
    assert h.thread.pending == {"slot": "matter", "doc_type": "Exhibits"}
    assert (await handle(pipeline, msg("help"))).reply.text.count("WATCH") >= 1
    done = ThreadState(thread_key="<t>", matter="M12205", doc_type="Exhibits", next_offset=3)
    thanks = await handle(pipeline, msg("thanks!", subject="Re: Documents"), thread=done)
    assert thanks.reply is None and thanks.silent_reason == "no_request"
    assert site.opened == 0


async def test_not_found_empty_tab_and_multi(env):
    _, _, _, pipeline = env
    nf = await handle(pipeline, msg("M99999 exhibits"))
    assert "couldn't find M99999" in nf.reply.text
    empty = await handle(pipeline, msg("transcripts for M12205"))
    assert "no Transcripts" in empty.reply.text and empty.attachments == []
    multi = await handle(pipeline, msg("Exhibits for M12205 and Key Documents for M12205"))
    assert len(multi.attachments) == 2
    assert "all 3 Exhibits" in multi.reply.text and "both Key Documents" in multi.reply.text


async def test_request_timeout_gives_honest_reply_with_trace(env):
    _, site, _, pipeline = env
    site.delay = 5
    pipeline.request_timeout_s = 0.05
    h = await handle(pipeline, msg(CANONICAL), trace_id="T-123")
    assert "isn't responding as expected" in h.reply.text
    assert "T-123" in h.reply.text and h.attachments == []


async def test_slow_job_sends_ack_first(env):
    _, site, _, pipeline = env
    site.delay = 0.2
    pipeline.ack_after_s = 0.05
    acks = []

    async def ack(requests):
        acks.append([r.matter for r in requests])

    h = await handle(pipeline, msg(CANONICAL), ack=ack)
    assert acks == [["M12205"]]
    assert h.attachments


async def test_watch_and_unwatch(env):
    _, _, ledger, pipeline = env
    w = await handle(pipeline, msg("WATCH M12205"))
    assert "M12205" in w.reply.text
    [watch] = ledger.list_watches("grader@example.com")
    assert watch.snapshot["counts"]["Other Documents"] == 14
    u = await handle(pipeline, msg("UNWATCH M12205"))
    assert u.reply is not None and ledger.list_watches() == []


async def test_get_command(env):
    _, site, _, pipeline = env
    doc = site.rows[DocType.KEY_DOCUMENTS][1].doc_no
    thread = ThreadState(thread_key="<t>", matter="M12205", doc_type="Other Documents")
    h = await handle(pipeline, msg(f"GET {doc}", subject="Re: Documents"), thread=thread)
    assert h.reply and h.attachments
    with zipfile.ZipFile(h.attachments[0]) as zf:
        assert any(n.startswith(doc) for n in zf.namelist())


def test_next_offset_counts_skipped_rows():
    from tests.fake_site import COUNTS, METADATA, make_rows
    from uarb_agent.models import MatterReport, Skipped

    rows = make_rows(DocType.OTHER_DOCUMENTS, 14)
    r = MatterReport(metadata=METADATA, counts=COUNTS, requested=DocType.OTHER_DOCUMENTS, rows=rows, trace_id="t")
    assert next_offset(r) == 0
    r.skipped = [Skipped(doc_no=rows[6].doc_no, title="x", reason="confidential")]
    assert next_offset(r) == 7


# --- the worker around the pipeline, with fake IMAP/SMTP -------------------------------------------------


@pytest.fixture
def mail(env, tmp_path):
    settings, site, ledger, pipeline = env
    server = FakeServer(tmp_path / "mail")
    transport = FakeTransport(tmp_path / "mail")
    outbox = Outbox(ledger, transport, tmp_path / "outbox")
    worker = Worker(pipeline, ledger, outbox, transport, agent_email=AGENT)
    return server, transport, outbox, worker


async def deliver_and_run(worker, server, outbox, raws):
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    for raw in raws:
        server.deliver(raw)
    box = SettlingMailbox(FakeMailbox(server), outbox)
    await asyncio.to_thread(lambda: worker.on_batch(box, box.fetch_unseen()))
    await worker.queue.join()
    # Next IMAP round: settle sent -> done, then label any duplicate copy of an answered message.
    await asyncio.to_thread(lambda: worker.on_batch(box, box.fetch_unseen()))
    await worker.queue.join()
    leftover = await asyncio.to_thread(box.fetch_unseen)
    stop.set()
    await runner
    return leftover


async def test_worker_replies_once_threaded_with_zip(mail, env):
    server, transport, outbox, worker = mail
    ledger = env[2]
    raw = make_email(body=CANONICAL, message_id="<q1@example.com>")
    leftover = await deliver_and_run(worker, server, outbox, [raw, raw])  # duplicate delivery
    assert leftover == []
    [sent] = server.sent()
    reply = email.message_from_bytes(sent, policy=policy.default)
    assert reply["In-Reply-To"] == "<q1@example.com>"
    assert reply["Auto-Submitted"] == "auto-replied" and reply["X-Agent-Trace"]
    assert [p.get_filename() for p in reply.iter_attachments()] == ["M12205_Other_Documents.zip"]
    assert ledger.get("<q1@example.com>").status == Status.DONE
    assert ledger.get_thread("<q1@example.com>").next_offset == 10
    assert not any(worker.pipeline.out_dir.rglob("*.zip"))  # working ZIPs removed once the .eml exists


async def test_worker_ignores_auto_replies_and_stays_silent_on_thanks(mail, env):
    server, transport, outbox, worker = mail
    ledger = env[2]
    ooo = make_email(body=CANONICAL, message_id="<ooo@x>", headers={"Auto-Submitted": "auto-replied"})
    thanks = make_email(body="thanks!", subject="Re: Documents", message_id="<ty@x>", headers={"In-Reply-To": "<q@x>"})
    ledger.save_thread(ThreadState(thread_key="<q@x>", matter="M12205", doc_type="Exhibits", next_offset=3))
    await deliver_and_run(worker, server, outbox, [ooo, thanks])
    assert server.sent() == []
    assert ledger.get("<ooo@x>").status == Status.IGNORED
    assert ledger.get("<ty@x>").status == Status.FAILED


async def test_worker_email_timeout_still_replies(mail, env):
    server, transport, outbox, worker = mail
    env[1].delay = 5
    worker.email_timeout_s = 0.1
    await deliver_and_run(worker, server, outbox, [make_email(body=CANONICAL, message_id="<slow@x>")])
    [sent] = server.sent()
    text = email.message_from_bytes(sent, policy=policy.default).get_body(("plain",)).get_content()
    assert "isn't responding as expected" in text


async def test_worker_apologises_for_exhausted_job(mail, env):
    server, transport, outbox, worker = mail
    ledger = env[2]
    raw = make_email(body=CANONICAL, message_id="<tired@x>")
    ledger.record("<tired@x>", uid="1", sender="grader@example.com")
    for _ in range(3):
        ledger.claim("<tired@x>")
        ledger.transition("<tired@x>", Status.WORKING, Status.RECEIVED)
    ledger.claim("<tired@x>")
    worker.exhausted = {"<tired@x>"}
    await deliver_and_run(worker, server, outbox, [raw])
    [sent] = server.sent()
    assert (
        "isn't responding as expected"
        in email.message_from_bytes(sent, policy=policy.default).get_body(("plain",)).get_content()
    )
    assert env[1].opened == 0


def test_parse_inbound_roundtrip_for_simulate():
    ib = parse_inbound("1", make_email(body=CANONICAL, message_id="<a@b>"))
    assert ib.thread_key == "<a@b>" and ib.sender == "grader@example.com"


# --- reliability regressions ------------------------------------------------------------------------------


async def test_parse_runs_off_the_event_loop(env, monkeypatch):
    """A hung LLM fallback (parse blocks ~30 s when the API is unreachable) must not freeze other jobs."""
    import time

    import uarb_agent.jobs.worker as worker_mod

    real = worker_mod.parse

    def slow_parse(*a, **kw):
        time.sleep(0.3)
        return real(*a, **kw)

    monkeypatch.setattr(worker_mod, "parse", slow_parse)
    gaps, last = [], [time.monotonic()]

    async def ticker():
        for _ in range(30):
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last[0])
            last[0] = now

    await asyncio.gather(handle(env[3], msg("help")), ticker())
    assert max(gaps) < 0.2


class FlakyTransport(FakeTransport):
    def __init__(self, root, failures):
        super().__init__(root)
        self.failures = failures

    def send(self, raw, from_addr, to_addrs):
        if self.failures > 0:
            self.failures -= 1
            raise OSError("smtp connection reset")
        super().send(raw, from_addr, to_addrs)


async def test_reply_left_composed_after_smtp_outage_is_sent_on_a_later_round(env, tmp_path):
    """A reply left composed by an SMTP outage must go out on a later IMAP round, without a restart."""
    _, _, ledger, pipeline = env
    server = FakeServer(tmp_path / "mail")
    transport = FlakyTransport(tmp_path / "mail", failures=1)
    outbox = Outbox(ledger, transport, tmp_path / "outbox")
    worker = Worker(pipeline, ledger, outbox, transport, agent_email=AGENT)
    leftover = await deliver_and_run(worker, server, outbox, [make_email(body="help", message_id="<flaky@x>")])
    assert len(server.sent()) == 1
    assert leftover == []  # settled on the final fetch
    job = ledger.get("<flaky@x>")
    assert job.status == Status.DONE
    assert not list((tmp_path / "outbox").glob("*.eml"))  # done replies do not pile up on disk


async def test_resend_checks_sent_mail_first(env, tmp_path):
    """SMTP can fail after the server accepted the message; a retry must not send a second copy."""
    _, _, ledger, pipeline = env
    server = FakeServer(tmp_path / "mail")
    transport = FakeTransport(tmp_path / "mail")
    outbox = Outbox(ledger, transport, tmp_path / "outbox")
    worker = Worker(pipeline, ledger, outbox, transport, agent_email=AGENT)
    real_send = outbox.send

    def accepted_then_reset(mid):
        real_send(mid)  # the bytes reach Sent Mail...
        ledger._db.execute("UPDATE jobs SET status = 'composed' WHERE message_id = ?", (mid,))
        raise OSError("connection reset during QUIT")  # ...but we never hear back

    outbox.send = accepted_then_reset
    await deliver_and_run(worker, server, outbox, [make_email(body="help", message_id="<dup@x>")])
    assert len(server.sent()) == 1
    assert ledger.get("<dup@x>").status == Status.DONE


async def test_crashed_job_is_retried_on_the_next_round(mail, env, monkeypatch):
    """A crash after the pipeline (e.g. disk full while writing the .eml) must not strand the job in `working`."""
    server, transport, outbox, worker = mail
    ledger = env[2]
    real = outbox.compose
    calls = []

    def compose_once_broken(mid, reply):
        calls.append(mid)
        if len(calls) == 1:
            raise OSError("No space left on device")
        return real(mid, reply)

    monkeypatch.setattr(outbox, "compose", compose_once_broken)
    await deliver_and_run(worker, server, outbox, [make_email(body="help", message_id="<crash@x>")])
    assert len(server.sent()) == 1
    job = ledger.get("<crash@x>")
    assert job.status == Status.DONE and job.attempts == 2


async def test_job_that_always_crashes_ends_failed_not_looping(mail, env, monkeypatch):
    server, transport, outbox, worker = mail
    ledger = env[2]
    monkeypatch.setattr(outbox, "compose", lambda mid, reply: (_ for _ in ()).throw(OSError("disk")))
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    server.deliver(make_email(body="help", message_id="<doomed@x>"))
    box = SettlingMailbox(FakeMailbox(server), outbox)
    for _ in range(MAX_ATTEMPTS + 2):
        await asyncio.to_thread(lambda: worker.on_batch(box, box.fetch_unseen()))
        await worker.queue.join()
    stop.set()
    await runner
    job = ledger.get("<doomed@x>")
    assert job.status == Status.FAILED and job.attempts == MAX_ATTEMPTS + 1


async def test_batch_note_only_when_a_second_batch_exists(env):
    # Regression: "send all the key docs" (2 of them here, 6 live) still said "reply MORE for the next batch".
    _, _, _, pipeline = env
    few = await handle(pipeline, msg("send all the key documents for M12205"))
    assert "all 2 Key Documents" in few.reply.text or "both" in few.reply.text
    assert "up to 10 documents per email" not in few.reply.text
    many = await handle(pipeline, msg("send all 14 other documents for M12205"))
    assert "up to 10 documents per email" in many.reply.text


async def test_more_after_get_continues_from_that_document(env):
    # Regression: GET's reply offered "MORE to get documents 3-..." of its tab, but the thread still pointed at
    # the earlier batch, so MORE answered something else.
    _, site, _, pipeline = env
    rows = site.rows[DocType.OTHER_DOCUMENTS]
    thread = ThreadState(thread_key="<t>", matter="M12205", doc_type="Other Documents", next_offset=10)
    got = await handle(pipeline, msg(f"GET {rows[0].doc_no}", subject="Re: Documents"), thread=thread)
    assert "Reply MORE to get documents 2-11" in got.reply.text
    more = await handle(pipeline, msg("MORE", subject="Re: Documents"), thread=got.thread)
    assert "documents 2-11 out of the 14 Other Documents" in more.reply.text


def test_request_timeouts_follow_the_tab_budget(env):
    _, _, _, pipeline = env
    assert pipeline._timeout(DocType.OTHER_DOCUMENTS) == 120
    assert pipeline._timeout(DocType.TRANSCRIPTS) == 160
    assert pipeline._timeout(DocType.RECORDINGS) == 280
    assert pipeline._timeout(DocType.RECORDINGS) < EMAIL_TIMEOUT_S
    pipeline.request_timeout_s = 0.05  # an explicit value wins for every tab
    assert pipeline._timeout(DocType.RECORDINGS) == 0.05


def _index_pdf(pipeline, tmp_path, doc_no: str, text: str) -> None:
    import pymupdf

    path = tmp_path / f"{doc_no}.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(path)
    doc.close()
    pipeline.fetcher.index.index_document(
        matter="M12205", tab=DocType.KEY_DOCUMENTS, doc_no=doc_no, path=path, title="Board Order", date="2026-07-08"
    )


async def test_search_command_cites_pages_and_offers_get(env, tmp_path):
    _, site, _, pipeline = env
    _index_pdf(pipeline, tmp_path, "102674", "Halifax Water is directed to continue to file semi-annual reports.")
    h = await handle(pipeline, msg("", subject="SEARCH semi-annual reports in M12205"))
    text = h.reply.text
    assert 'the 1 document (1 page) from M12205 that I\'ve already downloaded for "semi-annual reports"' in text
    assert "1. 102674 Board Order (M12205 Key Documents, Jul 8, 2026), page 1" in text
    assert "**semi-annual reports**" in text and "<b>semi-annual reports</b>" in h.reply.html
    assert "Reply GET 102674 from M12205 to get this document." in text
    assert h.attachments == [] and site.opened == 0  # the index only; the site is never touched
    assert h.thread.matter == "M12205"

    # The suggested follow-up parses as a GET of that document.
    follow = await handle(pipeline, msg("GET 102674 from M12205", subject="Re: SEARCH"), thread=h.thread)
    assert follow.reply is not None


async def test_search_with_nothing_downloaded_says_so(env):
    _, site, _, pipeline = env
    h = await handle(pipeline, msg("search for rate base"))
    assert "I haven't downloaded any documents yet" in h.reply.text
    assert site.opened == 0


async def test_downloads_become_searchable(env):
    _, _, _, pipeline = env
    await handle(pipeline, msg(CANONICAL))
    assert pipeline.fetcher.index.stats("M12205").documents == 10
