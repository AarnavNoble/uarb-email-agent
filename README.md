# UARB Email Agent

UARB Email Agent is an email agent for the public matter database of Nova Scotia's UARB (uarb.novascotia.ca). Email it a request in plain language, for example *"Other Documents from M12205, please"*. It opens the matter on the UARB site and reads what the matter is about and how many documents each tab holds. It then downloads up to ten documents of the type you asked for and replies in the same thread with a short summary and a ZIP.

> Draft. The results section is filled from measured runs only.

## What a reply looks like

```
M12205 is about Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project -
$69,275,000. It relates to Water within the Capital Expenditure Approvals category. The matter had an
initial filing on April 7, 2025 and a final filing on October 23, 2025. I found 13 Exhibits, 6 Key
Documents, 43 Other Documents, 0 Transcripts, and 0 Recordings. I downloaded 10 out of the 43 Other
Documents and attached them as a ZIP.

Attached:
- 102674 Board Order (Jul 8, 2026, 167 KB)
- ...
Reply MORE to get documents 11-20.
```

The ZIP holds the files, a `manifest.csv` (document number, title, date, security, size, SHA-256, source) and a `README.txt`.

## Architecture

```
 sender's mail client
        │  "Other Documents from M12205?"
        ▼
 Gmail inbox ── IMAP IDLE (re-armed every 55 s, polling fallback, catch-up on reconnect)
        │
 ┌──────┴──────────────────────── uarb-agent serve (one asyncio process under launchd) ───────────────┐
 │ mail/guards     drop bounces, auto-replies, list mail, our own mail; per-sender and per-thread caps│
 │ jobs/ledger     SQLite: received → working → composed → sent → done (compare-and-set transitions)│
 │ jobs/worker     bounded queue, 2 consumers, 90 s per request, 4 min per email, always replies    │
 │   intent/parser   rules first (regex, aliases, fuzzy typos); optional Haiku fallback, guarded     │
 │   core.fetch()    matter → MatterReport   ◄── the one contract every channel uses              │
 │     uarb/connector  Playwright on FileMaker WebDirect: open, metadata, counts, list, download   │
 │     cache/store     documents by checksum (immutable), listings for 10 minutes                  │
 │   pack/zipper     email-safe ZIP under the size budget, manifest + README                       │
 │   reply/render    text + HTML from templates; every number checked against the report          │
 │ mail/outbox     write the .eml first, then SMTP, then label the original done                    │
 │ jobs/watch      hourly: counts moved on a watched matter → "New filing on M…" email              │
 │ health          healthchecks.io ping while the inbox loop is healthy                             │
 └─────────────────────────────────────────────────────────────────────────────────────────────────┘
        ▲ same fetch()
   uarb-agent fetch / simulate / doctor (CLI)
```

## Components

| Job the agent has to do | Where it lives |
|---|---|
| Receive email and reply in the same thread | `uarb_agent/mail/inbox.py` (IMAP), `uarb_agent/mail/threading.py` (reply headers), `uarb_agent/mail/outbox.py` (SMTP, exactly once) |
| Understand what was asked | `uarb_agent/intent/parser.py`, `aliases.py`, `llm_fallback.py` |
| Find the matter on the site | `uarb_agent/uarb/connector.py` (`open_matter`), `browser.py`, `settle.py` |
| Read the matter details and the five tab counts | `uarb_agent/uarb/parse.py` (`parse_metadata`, `parse_counts`) |
| List and download documents | `uarb_agent/uarb/connector.py` (`list_documents`, `download`) and `uarb_agent/core.py` (which rows, the cache, retries) |
| Bundle the files | `uarb_agent/pack/zipper.py` |
| Write the summary | `uarb_agent/reply/render.py` and `uarb_agent/reply/templates/` |
| Keep it running | `uarb_agent/jobs/{ledger,worker,watch}.py`, `uarb_agent/health.py`, `deploy/` |

`uarb_agent/models.py` is the shared contract: `MatterReport`, `DocRow`, `FileResult`, `Request` and the typed errors (`MatterNotFound`, `EmptyTab`, `LayoutChanged`, `SiteUnavailable`, ...).

## Running it

Requirements: macOS or Linux, Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run playwright install chromium
cp .env.example .env          # fill in AGENT_EMAIL / AGENT_APP_PASSWORD to serve email

uv run uarb-agent fetch M12205 "Other Documents"          # ZIP + reply text, straight from the site
uv run uarb-agent parse "exibits for m-12205 please"      # what the parser understood
uv run uarb-agent simulate --from you@example.com \
    --subject "Documents" --body "Other Documents from M12205?"   # full pipeline, writes data/outbox_preview/*.eml, sends nothing
uv run uarb-agent doctor                                   # live canary: layout, counts, one download
uv run uarb-agent serve                                    # the email agent
```

`simulate` keeps thread state in `data/simulate.sqlite3`. To try a follow-up, pass `--in-reply-to <Message-ID of the first simulated email>` with a body such as `MORE` or `Exhibits`.

To run it as a service on a Mac, follow the install notes at the top of `deploy/com.uarb-email-agent.agent.plist` (KeepAlive, logs in `~/Library/Logs/uarb-email-agent/`). `deploy/com.uarb-email-agent.canary.plist` runs `uarb-agent doctor` every morning.

Tests: `make test` covers the unit, golden and chaos suites and needs no network, email account or API key. `make live` runs a few tests against the real site on M12205 and M99999.

### Things you can send

- A matter and a type in any wording: "key docs for m 12205", "exibits M12205".
- A matter number alone gets a summary with the counts, and an offer to send any type that has documents.
- In the same thread: `MORE` (the next ten), `GET 102674` (one document), or a one-word type after the agent asks.
- `WATCH M12205` / `UNWATCH M12205` to get, or stop, an email when a new document is filed.
- `HELP`.

## Design decisions

- **A browser, not an API.** The site is FileMaker WebDirect. Every data path runs over a websocket, and FileMaker's XML, Data API and OData endpoints are all off. Playwright drives the real UI, and `docs/RECON.md` records what that took: typing into a field that echoes back from the server, a virtualised grid, and downloads tied to the session.
- **Deterministic first.** Rules handle the parsing, and templates write the reply. The optional LLM fallback (off by default) may only fill a slot the rules left empty, and any matter number it returns must appear in the email. Every number in a reply must come from the data read off the site; otherwise the reply is not sent.
- **Exactly one reply.** The reply and its Message-ID are written to disk before SMTP. After a crash, the agent checks Sent Mail before resending, and the original email is labelled only after the send. A chaos test kills the process at each step.
- **Always answer.** Every request ends in a reply: documents, a question back, or an honest "the site isn't responding" with a trace reference. If a job is still running after 30 s, a short acknowledgement goes out first.
- **Polite to the site.** At most two browser sessions run at once, with a gap between them. Empty tabs are never clicked. Documents are cached by checksum, so asking twice does not download twice.
- **Confidential rows are skipped.** For those rows the public site serves a one-page notice in place of the document. The reply lists them as not included instead of counting the notice as a download.
- **Sized for Gmail.** ZIPs are capped at 16 MiB of files. Base64 makes that about 24 MB on the wire, under Gmail's 25 MB limit. Blocked file types and encrypted archives are never attached.

## Results

_To be filled from measured runs (bench over a set of matters, email round-trip p50/p95, parser accuracy)._

| Measure | Value |
|---|---|
| Live fetch, M12205 Other Documents, cold cache (10 files, 10.4 MB ZIP) | 30.8 s (open 7.5 s, list 4.4 s, downloads 18.1 s) |
| Same request, warm cache | about 1 s |
| `uarb-agent doctor` | 12.9 s |
| Matters benchmarked | _TBD_ |
| Email round trip p50 / p95 | _TBD_ |
