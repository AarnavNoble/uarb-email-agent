# UARB site recon

Notes from a read-only look at the Nova Scotia UARB matter database
(`https://uarb.novascotia.ca/fmi/webd/UARB15`), using only M12205 and M99999,
with a few seconds between runs and two documents downloaded in total.

## No API, so a browser it is

- The URL serves a FileMaker WebDirect app built on Vaadin 8. Plain HTTP gets a
  ~2 KB bootstrap shell with no data and a `JSESSIONID` cookie (12 h).
- All data moves over a Vaadin websocket (`/fmi/webd/PUSH`) with CSRF tokens and
  sequence ids, plus a heartbeat every 30 s. Replaying that protocol would be brittle.
- FileMaker's other publishing endpoints are off: `/fmi/xml/fmresultset.xml` returns
  FileMaker error 959 (XML publishing disabled); `/fmi/data/...` and `/fmi/odata/v4`
  return 502.
- Conclusion: drive the UI with Playwright (headless Chromium works).

## Finding a matter

1. Load the page and wait for `networkidle` once (home is ready in 3-4.5 s). After
   that, `networkidle` is useless because of the websocket, and the URL never changes.
   Waits use a "page text stable for ~1.2 s" helper instead.
2. The matter box is a FileMaker field, not an `<input>`:
   `div.fm-textarea:has(div.placeholder:text-is('eg M01234'))`. Click it, then wait for
   `document.activeElement.isContentEditable`.
3. Enter the number with a single `keyboard.insert_text()`. Per-key typing loses
   characters to server echoes: the server once received `05` and answered with a
   false "No Records Found". Reading the box back before searching catches this.
4. There are three "Search" buttons; the right one sits on the same row as the box
   (match by bounding-box y).
5. Outcome: the tab buttons appear (text contains `Exhibits - `) in 0.4-1.7 s, or a
   `.v-window` modal appears.

## Matter metadata

Laid out as labelled values on the overview: Matter No, Title, Type, Date Received,
Decision Date, Outcome on the first row; Status and Category on the second. Notes:

- The title includes the dollar amount, e.g. "... Redevelopment Project - $69,275,000".
- Category is visually truncated, but `innerText` holds the full value
  ("Capital Expenditure Approvals").
- The final date is labelled "Decision Date" on the overview and "Date Final
  Submissions" on tab layouts; same value. Dates are `MM/DD/YYYY`.
- FileMaker `fm_object_NNN` classes identify fields but differ per layout, so they are
  only a fallback.

## Tabs and counts

- Five tab buttons, text like `Other Documents - 43`, parsed with
  `^(Exhibits|Key Documents|Other Documents|Transcripts|Recordings)\s*-\s*(\d+)$`.
- Counts are live and change over time (M12205 was 13 / 6 / 43 / 0 / 0 at capture).
  Tests and demos must never hard-code them.
- Each tab is its own FileMaker layout with different field ids (the doc number is
  object 201 on Other Documents, 268 on Key Documents, 300 on Exhibits). Rows are
  therefore parsed by geometry and regex: the date and extension by pattern, the
  top-left box is the doc number, the widest top box is the title, the lower box is
  the security level.
- Exhibit numbers are not numeric (`H-4(C)-i`).
- Each tab shows "Found Count: N". Tabs take 1.4-2 s to open.

## The document grid

- A virtualised Vaadin grid: only ~12 `tr.v-grid-row-has-data` exist at once (68 px
  rows). No pagination.
- Scrolling `.v-grid-scroller-vertical` by 0.8 of its height and de-duplicating by doc
  number collected all 43 Other Documents in ~3.6 s; stop when the total matches Found
  Count.
- Rows are newest first. Security is per row; 7 of 43 Other Documents and 6 of 13
  Exhibits were Confidential.

## Downloading

- "GO GET IT" opens a "Download Files" modal (`.v-window`) within ~0.3 s; it does not
  download by itself.
- The modal holds `div.v-button.fm-download-button` captioned `<docno>.pdf`. Clicking
  it fires an ordinary browser download in 0.35-1.4 s from
  `/fmi/webd/APP/connector/0/<n>/dl/<docno>.pdf`. Then click Close.
- That URL is bound to the live session (403 outside it) and can't be predicted, so the
  download event must be captured in the browser.
- Two public PDFs were fetched (~170 KB each). Not yet checked: Confidential rows,
  modals with several files, and non-PDF Recordings.

## Error states

- Unknown matter (M99999): modal "No Records Found ... Click OK to return to Search
  page." within ~0.2 s.
- Empty tab: modal "No Matching Records ... Click Ok to return to Matter". Clicking OK
  returns to the overview. Better: never click a tab whose count is 0.
- Any open modal puts a `.v-window-modalitycurtain` over the page that blocks clicks,
  so unknown modals must be detected and reported rather than left to time out.

## Session behaviour

- `page.go_back()` leaves the app entirely; switch tabs with the tab buttons instead.
- Two concurrent browser contexts worked (~8 s each for open + list).
- WebDirect sessions likely count against the server's connection licence and linger
  after the browser closes, so the agent caps itself at two sessions with a gap between
  new ones.
- Cold browser to a full row list: ~10-12 s.

## Fixtures

`tests/fixtures/` holds the captured overview page (`matter.html`), the Other
Documents tab (`other.html`), a download modal (`modal_0.html`) and the scraped rows
(`rows_other.json`) for offline parser tests.
