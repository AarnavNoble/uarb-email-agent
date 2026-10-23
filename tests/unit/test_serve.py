"""`uarb-agent serve` wired end to end with fake IMAP, SMTP and site: one email in, one threaded reply out."""

import asyncio
import email
import os
import signal
import time
from email import policy

from tests.fake_site import FakeSite
from tests.mail_fakes import FakeMailbox, FakeServer, FakeTransport, make_email
from uarb_agent import cli
from uarb_agent.cache.store import Store
from uarb_agent.config import Settings
from uarb_agent.core import Fetcher


def test_serve_answers_one_email_and_shuts_down(tmp_path, monkeypatch):
    server = FakeServer(tmp_path / "mail")
    server.deliver(make_email(body="Key Documents for M12205 please", message_id="<s1@example.com>"))

    class Box(FakeMailbox):
        def __init__(self, *args, **kwargs):
            super().__init__(server)

        def connect(self):
            return self

        def wait(self, timeout):
            time.sleep(0.05)
            return True

    site = FakeSite()
    monkeypatch.setattr("uarb_agent.mail.inbox.ImapMailbox", Box)
    monkeypatch.setattr("uarb_agent.mail.outbox.SmtpSslTransport", lambda *a, **k: FakeTransport(tmp_path / "mail"))
    monkeypatch.setattr(cli, "Fetcher", lambda s: Fetcher(s, connector=site, store=Store(s.cache_dir)))
    settings = Settings(data_dir=tmp_path / "data", agent_email="agent@x.test", agent_app_password="pw")

    async def run():
        task = asyncio.create_task(cli._serve(settings))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if "1" in server.seen:
                break
        os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(task, 10)

    assert asyncio.run(run()) == 0
    [sent] = server.sent()
    reply = email.message_from_bytes(sent, policy=policy.default)
    assert reply["In-Reply-To"] == "<s1@example.com>"
    assert [p.get_filename() for p in reply.iter_attachments()] == ["M12205_Key_Documents.zip"]
    assert "uarb-agent/done" in server.labels["1"]
