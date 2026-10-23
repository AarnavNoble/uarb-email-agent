# UARB Email Agent

[![CI](https://github.com/AarnavNoble/uarb-email-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/AarnavNoble/uarb-email-agent/actions/workflows/ci.yml)

An email agent for the public matter database of the Nova Scotia Utility and Review Board
([uarb.novascotia.ca](https://uarb.novascotia.ca/fmi/webd/UARB15)). Email it a matter number and a document type in
plain language. It opens the matter on the UARB site, reads what the matter is about and how many documents each tab
holds, downloads up to ten documents of the requested type, and replies in the same thread with a summary and a ZIP.

Every document it downloads is also indexed page by page, so over time it builds a searchable database of UARB
filings: `SEARCH semi-annual reports in M12205` returns cited pages from that database.

**Try it:** email **uarb.email.agent@gmail.com**, for example *"Can you give me the Other Documents from M12205?"*
Send `HELP` to see everything it can do.

## What a reply looks like

```
Hi Jane,

M12205 is about Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000.
It relates to Capital Expenditure Approvals within the Water category. The matter had an initial filing on
April 7, 2025 and a final filing on October 23, 2025. The matter is closed; outcome: Directions given. I found
62 documents in total: 13 Exhibits, 6 Key Documents, 43 Other Documents, and no Transcripts or Recordings.
I downloaded 10 out of the 43 Other Documents and am attaching them as a ZIP here.

Board decision: approved $59,143,000 of $64,769,000 requested (Board Order 102674, July 8, 2026, pp. 1-2);
conditions: semi-annual (every six months) reports.

Attached:
- 102674 Board Order (Jul 8, 2026, 167 KB)
- 102454 HRWC (Board) Letter - Reply Submission (Jun 22, 2026, 163 KB)
- ...

Reply MORE to get documents 11-20.
Reply WATCH M12205 and I'll email you when something new is filed.
```

The ZIP contains the documents, a `manifest.csv` (document number, title, date, security level, size, SHA-256,
source) and a short `README.txt`. Files too large to email (some exhibits are 20-50 MB, recordings far more) are
published as download links that expire after 7 days. Confidential and Board Only documents, which the public site
replaces with a notice, are listed as skipped rather than counted as downloads.

## What you can send

| Request | Example | What happens |
|---|---|---|
| Documents | `Other Documents from M12205`, `exibits for m-12205` | Summary, counts, up to 10 newest documents as a ZIP |
| Summary only | `What's going on with M12205?` | Details and counts, plus an offer of the types that have documents |
| Next batch | `MORE` (in the same thread) | The next 10 |
| One document | `GET 102674` | That document |
| Search | `SEARCH rate base in M12383` | The 5 best-matching pages from downloaded documents, each cited with a `GET` line |
| Alerts | `WATCH M12205` / `UNWATCH M12205` | An email when a new document is filed |
| Help | `HELP` | The list above |

Unclear requests get a question back rather than a guess: a missing or malformed matter number, an unknown type, or
a tab with no documents (the reply suggests the types that do have some). Several requests in one email are answered
in one reply.

## How it works

```
 sender ──► Gmail inbox ── IMAP IDLE ──┐
                                       ▼
 ┌─────────────────────────── uarb-agent serve (one asyncio process, systemd) ───────────────────────────┐
 │ mail/guards      ignore bounces, auto-replies, mailing lists, our own mail; per-sender and thread caps│
 │ jobs/ledger      SQLite state machine per email: received → working → composed → sent → done        │
 │ jobs/worker      bounded queue, 2 consumers, time budget per request                                 │
 │   intent/parser    rules first (regex, aliases, fuzzy matching); Claude Haiku only for what's left   │
 │   core.fetch()     matter + type → MatterReport          ◄── the contract every channel uses         │
 │     uarb/connector   Playwright on FileMaker WebDirect: search, metadata, counts, list, download     │
 │     cache/store      documents by checksum, listings for 10 minutes                                  │
 │     search/index     SQLite FTS5, one row per PDF page                                               │
 │     extract/outcome  amounts, date and conditions from the Board's order, with page citations        │
 │   pack/zipper      email-safe ZIP + manifest; oversized files → expiring links (served by Caddy)     │
 │   reply/render     text + HTML templates; every number checked against the report before sending    │
 │ mail/outbox      write the reply to disk, send via the Gmail API, then mark the original done        │
 │ jobs/watch       hourly check of watched matters → "new filing" emails                               │
 │ health           healthchecks.io heartbeat; a failed send pings /fail                                │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────┘
                     ▲ same core.fetch()
   CLI: fetch, simulate, search, index, doctor        MCP server: get_matter, list_documents, download_documents, search
```

| Responsibility | Code |
|---|---|
| Receive and reply in the same thread | `uarb_agent/mail/` (`inbox.py`, `threading.py`, `outbox.py`, `gmail_api.py`) |
| Understand the request | `uarb_agent/intent/` (`parser.py`, `aliases.py`, `llm_fallback.py`) |
| Navigate the site, count and download | `uarb_agent/uarb/` (`connector.py`, `parse.py`, `browser.py`) and `uarb_agent/core.py` |
| Compress and deliver | `uarb_agent/pack/` (`zipper.py`, `links.py`) |
| Write the reply | `uarb_agent/reply/` (`render.py`, `templates/`) |
| Search and cite decisions | `uarb_agent/search/`, `uarb_agent/extract/outcome.py` |
| Run unattended | `uarb_agent/jobs/` (`ledger.py`, `worker.py`, `watch.py`), `uarb_agent/health.py`, `deploy/` |

`uarb_agent/models.py` defines the shared types (`MatterReport`, `DocRow`, `Request`, ...) and the typed errors
(`MatterNotFound`, `EmptyTab`, `LayoutChanged`, `SiteUnavailable`, ...). `docs/RECON.md` documents how the site
behaves.

## Design decisions

- **Drive the real site.** The site is FileMaker WebDirect: data moves over a stateful websocket, and the FileMaker
  XML, Data API and OData endpoints are disabled. The connector uses Playwright and handles the site's quirks: the
  matter field echoes keystrokes back from the server (so the number is inserted in one step and read back), the
  document grid is virtualised (rows are collected while scrolling until the site's "Found Count" is reached), and
  "Go Get It" opens a dialog whose download URL only works inside the session.
- **Deterministic where it matters.** Counts, dates, titles and amounts come from the site or the PDF and are never
  produced by a model. The renderer refuses to send a reply containing a number that is not in the report. The LLM
  only interprets wording the rules can't, and any matter number it returns must appear in the email.
- **Exactly one reply per email.** The reply is written to disk with its Message-ID before sending, and the
  original is marked done only afterwards. After a crash the agent looks for the reply in Sent Mail before resending.
  The Gmail API assigns its own Message-ID, so every reply also carries the original in `X-Agent-Reply-ID`, which
  is what the lookup searches. A chaos test kills the process between each step.
- **Always answer.** Every email gets documents, a question back, or an explicit "the site isn't responding" with a
  trace reference. If a request is still running after 75 s, a short acknowledgement goes out first.
- **Bounded time.** Each tab has a download budget (80 s; 120 s for Transcripts, 240 s for Recordings), and each
  file has its own deadline within it. Whatever arrived is sent, and `MORE` resumes where it stopped.
- **Honest search and citations.** Search covers only documents the agent has downloaded, and the reply says how
  many documents and pages that is. The Board decision line appears only when the order's own wording matches, and
  it quotes the amounts as written with the pages they came from.
- **Polite to the site.** At most two browser sessions at a time with a gap between them, empty tabs are never
  opened, and documents are cached by checksum.
- **Fits Gmail.** ZIPs are capped at 16 MiB of files (about 24 MB once base64-encoded, under Gmail's 25 MB limit).
  Blocked file types and encrypted archives are never attached.

## Running it

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/). The fetch, parse, simulate, search and test
commands need no email account or API key.

```sh
uv sync
uv run playwright install --with-deps chromium

uv run uarb-agent fetch M12205 "Other Documents"       # ZIP + reply text, straight from the site
uv run uarb-agent parse "exibits for m-12205 please"   # how a request is understood
uv run uarb-agent simulate --from you@example.com --subject "Documents" \
    --body "Other Documents from M12205?"              # full pipeline to an .eml in data/, nothing is sent
uv run uarb-agent search "semi-annual reports" --matter M12205
uv run uarb-agent doctor                               # live check: layout, counts, one download
```

To serve email, copy `.env.example` to `.env` and fill in the Gmail account and app password, then run
`uv run uarb-agent serve`. Before the first run, `uv run uarb-agent skip-backlog` marks mail already in the inbox as
handled. On hosts that block SMTP, run `uv run python scripts/gmail_auth.py` once to send through the Gmail API.

### Deployment

The live agent runs on a small Linux VM under systemd (`deploy/uarb-email-agent.service`), with Caddy serving the
download links over HTTPS (`deploy/caddy/`) and healthchecks.io alerting when the heartbeat stops or a send fails.

### Use it from Claude Desktop (MCP)

`uarb-agent mcp` exposes the same functions as read-only MCP tools over stdio. Add this to
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "uarb": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/uarb-email-agent", "run", "uarb-agent", "mcp"]
    }
  }
}
```

## Testing

```sh
make test     # unit, golden and chaos tests: no network, email account or API key needed
make lint     # ruff
uv run python scripts/eval_parser.py   # parser accuracy on 67 labelled emails
make live     # a few tests against the real site
```

- **Golden tests** pin the exact text and HTML of every kind of reply.
- **Chaos tests** crash the process at each step of sending and check that exactly one reply goes out.
- **Parser evaluation:** 67 of 67 labelled emails (typos, mobile replies, multi-request emails, follow-ups,
  prompt-injection attempts) parse correctly with rules alone.
- **Live tests over email:** `scripts/send_tests.py` emails the running agent from its own address (tagged so the
  loop guard lets it through) and collects the replies.

CI runs lint, the test suite and the parser evaluation on every push and pull request.

## Results

Measured on the deployed agent.

| Measure | Result |
|---|---|
| Matters exercised through the connector | 16, filed 2006-2026, including open matters and ones with Transcripts and Recordings |
| Live email tests (typos, empty tabs, unknown matter, multi-request, search, injection, ...) | 17 of 17 answered correctly, each with exactly one reply |
| Site time, M12205 Other Documents (10 files, 10 MB) | 14 s cached matter page, about 30 s cold |
| Site time, M12600 Transcripts (3 files) | 20 s |
| Site time, M12205 Exhibits (3 attached, 4 linked at 22-46 MB) | 71 s |
| Server reboot to serving again | 24 s |

## Known limitations

- Hearing recordings can be hundreds of megabytes (one is over 700 MB). The site serves them at about 1 MB/s, so
  the largest can't be downloaded within an email's time budget; the reply says so.
- Scanned PDFs without a text layer are listed in search results by title only; there is no OCR.
- Search covers documents the agent has already downloaded, not the whole UARB site.

## License

MIT
