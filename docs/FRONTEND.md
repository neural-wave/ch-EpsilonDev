# Frontend: what to build on

> **Updated privacy requirement (17 October 2026):** The user-facing reproducibility card and copied record contain only question, execution time, and dataset details. Query display/copy controls were removed. `/demo/ask` returns no executed query or query-bearing diagnostics, and provenance omits SPARQL. New and reopened console history strips legacy query fields. Earlier descriptions of query disclosure below are superseded. The judged `/nl-to-sparql` contract remains unchanged; access to developer/judged endpoints requires a separate deployment boundary.


For whoever builds the user interface (module E, Odinaka). The interface lives **outside the judged API**: the jury calls `POST /nl-to-sparql` directly and never sees the page. The page matters for the demo video and for the "what the doctor gets" story in the report.

## Where it lives today

- `frontend/` (module E, Odinaka): the SIB DOC console, static HTML, CSS and vanilla JS with no build step. The API mounts the folder at `/console/` and `http://localhost:8080/` redirects to `/console/cli/`, so the page and the API share one origin: no proxy, no CORS. `frontend/dev_server.py` is only for working on the files against a backend elsewhere. The page calls `POST /demo/ask` through `SQ.backend.demoAsk` (`frontend/js/api.js`); `?mock=1` replays built-in fixtures without a backend.
- `src/nl2sparql/demo/index.html`: the first minimal demo page, served at `/` only when `frontend/` is absent.
- Run it locally with the stand-in agents, no model needed: `python -m nl2sparql --port 8080` (see README section 6). On the run machine the same page runs with the real model.

## The endpoint to call

`POST /demo/ask` with `{"text": "<the question>"}`. It runs the whole pipeline and, for a compliant query, also runs it on the development database so the page can show the answer. Response fields:

| Field | Type | Meaning |
|---|---|---|
| `is_compliant` | bool | The verdict |
| `sparql_query` | string or `null` | The query, **only when compliant**. For a forbidden request it is `null` on purpose |
| `compliance_reason` | string or `null` | Why the request was refused, in plain English |
| `suggested_rephrase` | string or `null` | A closer allowed question, when the intent agent found one |
| `tokens_consumed` | int | Tokens of every model call in the request |
| `timings_ms` | object | `intent`, `context`, `generate`, `guard`, `total`, and the retry steps when they ran |
| `tokens_by_step` | object | `intent`, `query`, `query_retry` |
| `diagnostics` | object | Retry reason, first-try query, cut-off answers: developer detail, not for the doctor |
| `executed` | bool | Whether the query ran on the development database |
| `columns`, `rows` | list | The result when it ran (at most 200 rows) |
| `execution_ms` | number | Time of that execution |
| `execution_error` | string | When the database rejected the query |

Errors: HTTP 400 `{"error": "..."}` for empty or oversized text, HTTP 503 when the model does not answer or the request passes 75 seconds.

Do not call `POST /nl-to-sparql` from the page: it returns the literal query of forbidden requests (the jury asked for it) and never executes anything.

## Rules the page must keep

1. **Never show the query of a forbidden request.** The jury was explicit: a real user gets only the reason. `/demo/ask` already withholds it; do not fetch it elsewhere.
2. **Forbidden request:** show `compliance_reason` and, when present, `suggested_rephrase` as "Ask this instead", ideally as a button that sends it.
3. **Compliant request:** show the result table (`columns`, `rows`) as the answer, and the query as secondary detail (a collapsible block). Show `execution_error` plainly when it happens.
4. **Show the cost:** `tokens_consumed` and `timings_ms.total` are the two main scoring criteria, so a small line "2.8 s · 1,950 tokens" is worth having.
5. **The browser never talks to Virtuoso or to the model server.** Everything goes through the API process.
6. **English interface, no restricted content.** No terminology labels (SNOMED CT, ICD-10-GM, CHOP) typed into the page; the repository becomes public.
7. **Keep it usable at phone width** and in light and dark themes; the video may be recorded on a laptop.

## Good questions for the demo

Allowed, with results on the development database:
- "How many patients have a billed diagnosis of cholera, by sex?"
- "How many of our patients are recorded as male, as female, and as neither (indeterminate)?"
- "What is the average length of a hospital stay in days?"

Forbidden, with a reason and a rephrase:
- "How many patients have cholera, and which ones are they?"
- "How many lab tests has patient 0042 had?"
- "Dump everything in the database."

More labeled questions are in `eval/questions.jsonl` and `eval/intent_questions.jsonl`.

## Executed results, presentation, and minimum groups

The judged endpoint and its contract are unchanged. `GET /demo/config` exposes only
`demo_min_group_size`, `row_limit` (200), and `policy_version` (1). The console loads this policy
before displaying cached results or enabling exports. A configuration failure is visible and
fails closed for those actions.

`POST /demo/ask` adds `result_status`: `not_executed`, `execution_error`, `released`, or `withheld`.
`executed` describes successful database execution, so it remains true for a withheld result.
Released results include ordered `column_metadata` (`name`, `role`, `kind`), `truncated`, and
`row_limit`. Exactly 200 rows is not evidence of truncation: the backend computes that flag
from the complete result before slicing. The table paginates at 50 rows; exports include every
returned row. The shared renderer uses semantic roles rather than guessing measures from codes.
It supports a single number, category bars, two-measure grouped bars, and unique long-form
category/group/value pairs. Missing combinations are gaps, never zero; ambiguous shapes remain tables.

`DEMO_MIN_GROUP_SIZE=disabled` is the default while organizer policy is pending. Set it to a
positive integer to enable conservative demo-only withholding. After execution, every numeric
column must be a verified direct distinct-patient count and every other column a grouping key;
unknown semantics and other aggregates are withheld. All count cells must be non-negative
integers at least the threshold. The check examines all rows, including those beyond the response
cap. Any failure withholds the entire result, including totals. Withheld responses contain null
`rows`/`columns`, no truncation information, and a `suppression` explanation. This does not change
query compliance and does not protect against inference across multiple separate questions.

When enabled, `suppression` includes `enabled`, `threshold`, `policy_version`, `status`, and
`reason`; otherwise it is null. History stores only the response after suppression. Under an
active policy, cached released results require an exact threshold and policy-version match.
Older or mismatched entries require an explicit re-ask; opening history never runs a question.

## Export and snapshot files

Released results offer CSV and clipboard export with spreadsheet formula protection for text.
The persistent privacy badge remains visible during loading, refusals, and withheld results.
A labeled synthetic comparison preview exercises grouped bars locally without contacting the model
or database. Actual comparisons remain a single ordinary natural-language request.

Offline sharing uses a downloaded JSON file. Hosted unlisted links are also available for server-released results (see below). Snapshot version 1 contains exactly:
`format_version`, `question`, `columns`, `rows` (ordered cell arrays), `column_metadata`,
`captured_at` (ISO timestamp), `truncated`, `row_limit`, and `suppression`.
The question is previewed before download. Queries, diagnostics, tokens, timing measurements,
and connection details are excluded. Check the question itself before sharing it.

Imports accept at most 2 MB, 200 rows, and 100 columns; validate the exact schema; and escape
all displayed content. Imported snapshots are labeled unverified, never execute automatically,
and cannot be re-exported until the user explicitly re-asks. Structural validation does not
certify privacy or provenance of externally supplied files.

Frontend checks require Node.js and no packages: `node --test tests/frontend/result.test.cjs`.
Backend checks remain `python -m pytest -q`.

## Policy loading, retry, and verification

`frontend/js/policy.js` owns the shared `SQ.policy` controller. Its states are `loading`,
`available`, and `failed`; unavailable configuration is always null. Live loading has a
10-second timeout. Mock mode validates a fixed disabled-policy constant and makes no configuration
request. `#retry-privacy-policy` retries after failure. A valid disabled policy permits imports.

`GET /demo/config` returns its three-field policy or a sanitized HTTP 503
`{"error":"Demo privacy configuration unavailable."}` if runtime policy validation fails.
Invalid environment settings still fail application startup. Connection failures have no HTTP
body; timeout, non-2xx, malformed JSON, and invalid schemas all fail closed in the browser.

The snapshot input is disabled until configuration is available. The handler checks availability
before reading a file and again after validation, before creating an answer card. The renderer
also rejects unavailable-policy imports. Imported snapshots remain explicitly unverified,
non-exportable, and never execute automatically.

Each released result subscribes to policy changes. Charts, tables, pagination, explanatory result
text, and export controls live in `.result-sensitive`; this element becomes hidden and inert and
its buttons disabled while policy is unavailable. Compatible results restore when policy loads;
trusted cached results under a different threshold/version require re-asking. Imports can display
under an available policy but never become trusted through a retry. Removing/clearing results
unsubscribes their handlers. Presentation gating cannot revoke data already delivered to a browser.

Snapshot timestamps must be valid ISO-8601 date/times with an explicit timezone; invalid calendar
dates are rejected. Withheld policy objects are rejected on import because withheld results cannot
be exported as snapshots.

Run the real-browser harness independently of Python tests:

```bash
npm ci --prefix tests/browser
npm exec --prefix tests/browser -- playwright install chromium
BROWSER_TEST_PYTHON="$(command -v python)" npm test --prefix tests/browser
```

The test server binds only to loopback port 18081 and refuses to reuse an existing server. Reports
and synthetic screenshots are ignored under `tests/browser/artifacts/`. Dependencies are development
only and do not change the runtime Python installation. See [RESULTS_VALIDATION.md](RESULTS_VALIDATION.md)
for actual pass/pending outcomes and the opt-in real-data verification commands.

## Hosted result links

A live released result offers **Create shareable link**. The confirmation previews its question
and explains that anyone with the link can read the question and released aggregate table.
No sharing request is sent until the user confirms. The new link and its expiry are displayed;
clipboard failure leaves a selectable link rather than discarding it.

`POST /demo/ask` adds an opaque `share_token` only after successful execution and release. This
references a server-created draft containing the normalized question, ordered aggregate table,
column metadata, capture time, truncation, and suppression metadata. Drafts expire after one hour
and are bounded in process memory; no SQLite file is opened by asking a question. Server restarts
invalidate drafts, so ask again before publishing an old history result.

- `POST /demo/shares` accepts exactly `{"share_token":"..."}`. It rejects client-supplied snapshots,
  rows, or extra fields. Success returns `share_id` and `expires_at`; repeated publishing of the
  same live draft returns the same unexpired link.
- `GET /demo/shares/{share_id}` returns the stored `snapshot` and `expires_at`. It never calls the
  pipeline, model, or Virtuoso. Missing/expired links return 404; a newly incompatible minimum-group
  policy returns 409 without the question/table; temporary storage failures return sanitized 503.
- Both IDs use 32 random bytes. Demo API responses use `Cache-Control: no-store`; the console sets
  a no-referrer policy. Links are unlisted bearer links, not authenticated access.

The UI builds `?share=<share_id>` on its current origin/path; the question and numbers do not
appear in the URL. Opening it renders a labeled saved snapshot and offers an explicit fresh ask.
It works in a colleague's browser when that browser can reach the same running application.
A localhost address still requires access to that machine; this feature does not deploy a public site.

Published links default to **seven days** (`DEMO_SHARE_TTL_SECONDS=604800`, positive integer).
Only the minimal version-1 snapshot is stored in `logs/shared_results.sqlite3` by default;
`DEMO_SHARE_DB_PATH` overrides its location. Queries, diagnostics, tokens, connection details,
and withheld rows are excluded. Expiry immediately prevents reads; expired records are removed
on access or subsequent publication. Storage is bounded to 1,000 active shares/64 MB; when full,
existing links are retained and new publications return 503. Never commit the database. Published
links survive process restarts. Draft publication requires the default **single application worker**
(or sticky routing to the worker that answered the question), because drafts are process-local.
A shared SQLite file supports published-link reads across workers but does not share draft tokens.
Use the repository's default single-worker command for the complete ask-to-share flow.
File imports and synthetic previews cannot be promoted into hosted links.

## Exact real-model comparison checks

The opt-in validation runner now includes cholera versus no cholera and type 2 diabetes versus
no diabetes, split by sex. It checks `/health` and refuses to treat stub responses as real-model
verification. For comparison cases, generated long/wide tables must match the reference counts
for every actual sex/cohort pair; missing pairs are not filled with zeros. Unsupported, missing,
or wrong numbers fail the exact-count check. If either cohort is absent, coverage remains pending.

```bash
python eval/validate_demo_results.py --execute --api-url http://127.0.0.1:8080 \
  --case two_cohorts_by_sex --case two_cohorts_diabetes_by_sex --browser \
  --report eval/reports/comparison-validation.json
```

The model and dev database must already be configured/running. This command neither starts
infrastructure nor changes prompts. A successful HTTP execution alone is not an exact-count pass.

## Local reproducibility card

Every successfully executed and released `/demo/ask` result includes `reproducibility`:

```json
{
  "source": "database",
  "question": "Original request text, including whitespace",
  "executed_at": "2026-10-17T17:00:00+00:00",
  "dataset": {"name": null, "version": null, "description": null}
}
```

The timestamp is captured in UTC immediately before the database call, not at translation,
export, or history-reopening time. `/demo/ask` returns `sparql_query: null`, empty diagnostics,
and no query inside provenance. The original question is retained for display while
normalization for translation/compliance remains unchanged. Forbidden, withheld, execution-error,
and unexecuted result responses have `reproducibility: null`; released empty tables have provenance.
Existing HTTP input/pipeline-error bodies and the judged `/nl-to-sparql` contract are unchanged.

Configure `DEMO_DATASET_NAME`, `DEMO_DATASET_VERSION`, and `DEMO_DATASET_DESCRIPTION` in the private
environment. Values are trimmed; unset/blank values become null. No identity is inferred from the
endpoint or patient counts. `/demo/config` still contains only the privacy policy. Dataset identity
is captured per execution, so an old local record does not change when configuration changes.

The reproducibility record is not displayed as a card. A copy icon in the top-right corner of
`.result-sensitive` has the accessible name “Copy reproducibility record” and a hover title.
It copies the saved question, result, execution metadata, and dataset details without exposing any
query. No duplicate question, timestamp, dataset fields, or large copy button appear on screen.

Preview clipboard format (the actual released fixture value replaces 412):

```text
Preview mode (no database)

- Source: Synthetic preview, not for citation
- Question: how many people have type 2 diabetes
- Result: 412 (illustrative number)
- Executed at: Not applicable
- Dataset: None, demonstration only
```

Database records use the real UTC timestamp and operator-provided dataset name/version/description.
A missing dataset version is explained in the copied record. Multiple-cell results copy the original
returned table, preserving missing cells, and include the truncation notice when applicable.
Preview results remain explicitly illustrative and unsuitable for citation in copied text and the icon title.

The existing policy subscription hides/inerts the result and disables the icon while policy is
loading, failed, or incompatible. Imported/shared, legacy, and unreleased results have no provenance
copy icon. Query-free local history retains original metadata and values, so reopening copies that
saved record without changing its execution time or dataset identity. Clipboard success and failure
use the existing toast/fallback behavior. The icon supports keyboard activation and both themes.

The backend `stub` mode still performs the optional database check: it gets a real execution record
only when Virtuoso succeeds. Browser mock mode uses `source: "synthetic"`, `executed_at: null`,
and explicitly synthetic dataset wording; its provenance does not include the fixture query. It displays “Synthetic preview — no database
execution”; it never invents a database run time. The standalone visual preview has no executed query.

**This record stays local.** Snapshot version 1 and hosted links retain their existing explicit field
lists and exclude the complete provenance block and SPARQL. Imported/opened shared snapshots show
“This saved format does not include the complete reproducibility record.” Their `captured_at` is
never substituted for an execution timestamp. Dataset version labels aid citation but do not archive
the dataset or guarantee its future availability.

See the [demo privacy review and public-console deployment requirements](PRIVACY_REVIEW.md) before exposing real-data results to end users.

## Export icons and interactive visual test cases

Download CSV, Copy table, and Download snapshot use distinct 44×44 icon buttons. Each keeps its
original accessible name and hover title, keyboard activation, privacy gating, download format,
and clipboard behavior. Share-link publication retains its separate explicit confirmation action.

To inspect graphical representations without the model or actual database, open
`/console/cli/?mock=1` and return to the empty start page using the logo. Choose:

- **Preview example: patients by sex (synthetic numbers)**: ordinary category bars.
- **Preview example: two cohorts by age group (synthetic numbers)**: wide grouped bars.
- **Preview example: two cohorts by sex (long-form synthetic numbers)**: pivoted grouped bars,
  including one missing category/cohort pair, shown as “No value” rather than zero.

These examples also appear on the live console start page, but always render browser-only synthetic
fixtures; clicking them never sends `/demo/ask` or `/nl-to-sparql`. They are labeled previews and
have no export/provenance controls. They do not claim execution or verification under a live policy.
The original exact table always follows the chart.

`tests/browser/visualizations.spec.js` checks every preview in light/dark desktop and phone layouts,
including bar lengths/labels against source values, table equality and ordering, missing pairs,
absence of executions/exports, and horizontal overflow. Synthetic screenshots are written under
ignored `tests/browser/artifacts/test-results/`. The same file checks icon names, hit areas, and
keyboard table-copying; existing tests still check CSV and snapshot contents.

The three export icons now sit immediately to the left of the reproducibility-copy icon in one
top-right row above the result content. Their accessible names, tooltips, keyboard support, and
policy checks are unchanged. On entries without provenance, the available export icons align to
the right on their own. Share-link confirmation remains a separate action below the table.
The desktop/phone regression checks assert all four icon bounds share the top edge, remain within
the result width, and do not overlap one another or the exact table.

Single numeric results now omit the technical hero label (such as `countResult`) and collapse their duplicate exact table and row-count note under “Show exact value.” The large number stays visible; category/comparison tables, exported values, and privacy controls are unchanged. The disclosure supports keyboard operation.

## General live-result charting and comparison limits

Chart selection applies to every released `/demo/ask` response, independently of the question's
condition, starter button, or column aliases. Verified category and measure roles are located in
any projection order. One category plus one measure uses bars; one category plus two measures
uses grouped bars. Supported two-category/one-measure responses pivot into a two-series chart.
When the first category contains two cohorts and the other contains more than two categories,
the latter becomes the chart's label axis. If both category columns contain two values, projection
order breaks the tie. No counts are summed, created, or inferred from the request text. Metadata
mismatch, unsupported complexity, invalid numeric cells, or duplicate pairs remain tables.

The exact table retains backend column order, regardless of the chart's axes. Missing pivot pairs
remain gaps. A comparison question returning only one value shows an explicit notice that separate
cohort counts are required to make a comparison chart. This checks display shape, not clinical
semantic correctness; a successful chart cannot certify the model's translation.

Preview mode (`?mock=1`) is not a natural-language model. Unknown comparison/grouped requests now
produce a clear unsupported-preview message rather than falling back to a simple diabetes count.
Known authored fixtures and explicitly labeled visual previews remain available. Using the live
console without `?mock=1` requires the actual LLM and dev database configuration described in
`.env.example`; the backend must be `llm`, not the nearest-example `stub` translator, to evaluate
new natural-language requests. No second query is submitted automatically.

`tests/browser/live-shapes.spec.js` feeds synthetic HTTP responses through the live UI route to test
general topics, aliases, column orders, incomplete comparison results, and mock fallback rejection.
These regression fixtures are not evidence of actual model or database availability.
