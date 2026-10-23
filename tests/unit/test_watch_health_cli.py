import email
from email import policy

from tests.fake_site import COUNTS, METADATA, FakeSite, make_rows
from uarb_agent import cli
from uarb_agent.cache.store import Store
from uarb_agent.config import Settings
from uarb_agent.core import Fetcher
from uarb_agent.health import Healthcheck
from uarb_agent.jobs.ledger import Ledger
from uarb_agent.jobs.watch import alert_message, changes, check_watches, snapshot
from uarb_agent.models import DocType, MatterReport

OTHER = DocType.OTHER_DOCUMENTS


def report(counts=COUNTS) -> MatterReport:
    return MatterReport(metadata=METADATA, counts=counts, trace_id="t")


def test_changes_lists_moved_tabs_in_site_order():
    before = snapshot(report())
    after = report({**COUNTS, OTHER: 15, DocType.EXHIBITS: 2})
    assert changes(before, after) == {DocType.EXHIBITS: (3, 2), OTHER: (14, 15)}
    assert changes(before, report()) == {}


async def test_check_watches_alerts_once_with_the_new_rows(tmp_path):
    settings = Settings(data_dir=tmp_path)
    site = FakeSite()
    fetcher = Fetcher(settings, connector=site, store=Store(settings.cache_dir))
    ledger = Ledger(tmp_path / "l.sqlite3")
    old = {**COUNTS, OTHER: 13}
    ledger.add_watch("a@b.c", "M12205", {"counts": {str(k): v for k, v in old.items()}})
    sent = []

    async def send(to, reply):
        sent.append((to, reply))

    assert await check_watches(ledger, fetcher, send, now=ledger.now() + 3600) == 1
    [(to, reply)] = sent
    assert to == "a@b.c" and reply.subject == "New filing on M12205"
    assert site.rows[OTHER][0].doc_no in reply.text
    # Snapshot updated: the next check is quiet, and one inside the hour is not even made.
    assert await check_watches(ledger, fetcher, send, now=ledger.now() + 7200) == 0
    opened = site.opened
    assert await check_watches(ledger, fetcher, send, now=ledger.now() + 10) == 0
    assert site.opened == opened

    msg = alert_message("a@b.c", "agent@x.test", reply)
    assert msg["Auto-Submitted"] == "auto-generated" and msg["Subject"] == "New filing on M12205"


def test_snapshot_remembers_newest_listed_doc():
    r = report()
    r.requested = OTHER
    r.rows = make_rows(OTHER, 3)
    assert snapshot(r)["newest"] == {"Other Documents": r.rows[0].doc_no}


def test_healthcheck_noop_throttle_and_errors():
    assert Healthcheck(None).ping() is False
    calls, now = [], [0.0]

    def opener(url, timeout):
        calls.append(url)

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Resp()

    h = Healthcheck("https://hc-ping.com/abc/", clock=lambda: now[0], opener=opener)
    assert h.ping() and not h.ping()
    now[0] = 61
    assert h.ping() and h.fail()
    assert calls == ["https://hc-ping.com/abc", "https://hc-ping.com/abc", "https://hc-ping.com/abc/fail"]

    def broken(url, timeout):
        raise OSError("offline")

    assert Healthcheck("https://x", opener=broken).ping() is False


def test_cli_parse(capsys):
    assert cli.main(["parse", "can you give me a list of Other Documents files from M12205?"]) == 0
    out = capsys.readouterr().out
    assert '"matter": "M12205"' in out and '"doc_type": "Other Documents"' in out


def test_cli_doc_type_aliases():
    assert cli._doc_type("Other Documents") is OTHER
    assert cli._doc_type("exibits") is DocType.EXHIBITS
    assert cli._doc_type(None) is None


def test_cli_serve_needs_credentials(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: Settings())
    assert cli.main(["serve"]) == 2
    assert "AGENT_EMAIL" in capsys.readouterr().err


def test_cli_simulate_writes_eml_without_sending(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path)
    site = FakeSite()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "Fetcher", lambda s: Fetcher(s, connector=site, store=Store(s.cache_dir)))
    argv = ["simulate", "--from", "Grader <g@example.com>", "--subject", "Docs", "--body", "Exhibits from M12205"]
    assert cli.main(argv) == 0
    [eml] = (tmp_path / "outbox_preview").glob("*.eml")
    reply = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert reply["To"] == "g@example.com" and reply["Subject"] == "Re: Docs"
    assert [p.get_filename() for p in reply.iter_attachments()] == ["M12205_Exhibits.zip"]
    out = capsys.readouterr().out
    assert "I downloaded all 3 Exhibits" in out
    assert "Hi Grader," in out  # greeted by name, as the worker does


def test_cli_index_then_search(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path)
    site = FakeSite()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "Fetcher", lambda s: Fetcher(s, connector=site, store=Store(s.cache_dir)))
    assert cli.main(["fetch", "M12205", "Exhibits", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "I downloaded 2 out of the 3 Exhibits" in out
    assert "taking a while" not in out  # --limit 2 is what was asked for, not a time-budget stop
    assert cli.main(["index"]) == 0  # the fetch already indexed both files; a backfill adds nothing
    assert capsys.readouterr().out.startswith("Indexed 0 new pages. 2 files from 1 matters")
    assert cli.main(["search", "anything", "--matter", "M12205"]) == 0
    assert 'No pages in the downloaded documents match "anything".' in capsys.readouterr().out


def test_cli_has_an_mcp_command():
    assert cli.build_parser().parse_args(["mcp"]).cmd == "mcp"
