# Result presentation verification record

> **Updated privacy requirement (17 October 2026):** The user-facing reproducibility card and copied record contain only question, execution time, and dataset details. Query display/copy controls were removed. `/demo/ask` returns no executed query or query-bearing diagnostics, and provenance omits SPARQL. New and reopened console history strips legacy query fields. Earlier descriptions of query disclosure below are superseded. The judged `/nl-to-sparql` contract remains unchanged; access to developer/judged endpoints requires a separate deployment boundary.


Initial status date: **17 October 2026**. Verification target: **18 October 2026, 11:00 Europe/Zurich**, the submission deadline documented in README.

Implementation/check owner: **implementing agent / code author (Codex)**. Run-machine coordination owner: **Santiago**, as documented in the repository. No handoff message has been sent automatically.

Code delivery accepts explicitly recorded **PENDING** external checks. It does not constitute a fully verified delivery. Fully verified status requires every real-data and browser check below to pass. Do not replace a pending status with a scheduled date or an assumed success.

Checkout at the first real-data attempt: `e67edfd69c837017304da1a26e2ff720e315ed3f` **plus uncommitted implementation changes**. Re-record the commit and dirty-worktree status on the run machine; the base commit alone does not identify these edits.

## Local verification

| Check | Status | Latest attempt | Evidence |
|---|---|---|---|
| Python unit/API tests | PASS | 17 October 2026, 20:59 CEST | `PYTHONPATH=src .venv311/bin/python -m pytest -q`: **788 passed** |
| Node unit tests | PASS | 17 October 2026, 20:59 CEST | `node --test tests/frontend/*.test.cjs`: **16 passed** |
| Chromium desktop and 390×844 phone | PASS | 17 October 2026, 20:59 CEST | **88 passed**; `tests/browser/artifacts/junit.xml`, HTML report, and synthetic screenshots |

Browser artifacts contain synthetic fixtures only and are git-ignored. The tests start their own stub application, refuse an existing server, and never use live patient data.

## External verification

All rows below are owned by Codex/code author, coordinated with Santiago. Target for each is the submission deadline above.

| Check | Status | Latest attempt (Europe/Zurich) | Missing prerequisite / sanitized outcome | Evidence |
|---|---|---|---|---|
| Three reference queries and both release-policy modes | PENDING | 17 October 2026, 19:02:48 | Configured local dev Virtuoso unavailable; no service was started | `eval/reports/demo-validation.json` |
| Three separate natural-language translations | PENDING | 17 October 2026, 19:02:48 prerequisite check | Authorized running LLM-backed demo API not configured; no NL requests sent | Same sanitized report |
| Browser equality against actual reference rows | PENDING | 17 October 2026, 19:02:48 prerequisite check | Requires dev Virtuoso and authorized demo API; synthetic browser tests are separate | Same sanitized report |
| Pipeline evaluation and baseline comparison | PENDING | Not executed; prerequisite check on 17 October 2026 | Current environment uses stub agents and has no reachable dev Virtuoso/model service | Run-machine pipeline report required |

Latest actual attempt (also repeated outside the network sandbox to distinguish unavailable services from sandbox restrictions):

```bash
PYTHONPATH=src .venv311/bin/python eval/validate_demo_results.py --execute --timeout 2
```

The local browser run used `BROWSER_TEST_PYTHON="$PWD/.venv311/bin/python"` and `PLAYWRIGHT_BROWSERS_PATH=/private/tmp/ep-rep-playwright`; both desktop and phone projects passed. `npm ci` reproduced the locked dependency installation. Light/dark phone screenshots were inspected. One existing Starlette/AnyIO deprecation warning remains in the Python suite.

The runner returned pending statuses rather than asserting that real-data checks passed. It stores only timestamps, commit information, check names, and sanitized outcomes. Exceptions, endpoints, questions, and executed result rows are not written to that report.

## Commands to resolve pending checks

Use the existing authorized run-machine environment, with its model connection and dev database already running. Do not run `runbox.sh up`, create tunnels, restart containers, or administer the GPU for this validation.

```bash
python -m pytest -q
node --test tests/frontend/*.test.cjs
npm ci --prefix tests/browser
npm exec --prefix tests/browser -- playwright install chromium
BROWSER_TEST_PYTHON="$(command -v python)" npm test --prefix tests/browser
python eval/validate_demo_results.py --execute --api-url http://127.0.0.1:8080 --browser
python eval/run_eval.py --execute
```

The reference runner defines a direct cholera patient count, cholera counts by sex, and cholera versus no-cholera counts by sex. It checks raw executed rows against released rows, semantic roles, the 200-row cap, and threshold 5 with an independent count-value oracle. `--api-url` also checks NL translation/execution independently; execution success alone is not proof of clinical semantic equivalence. `--browser` passes reference aggregate responses to `tests/browser/live-check.cjs` over stdin, checks actual table cells/chart labels, and writes no raw result artifacts. A dataset that cannot exercise the two-group chart leaves that coverage pending.

Exit codes: **0** all requested checks pass; **1** a check fails; **2** checks are pending or command arguments are invalid. Running without `--execute` contacts no services.

For the default shared evaluation set, compare with README section 11's **31-question** baseline: **90.3%** verdict accuracy, **0** forbidden requests marked compliant, **3.1 s / 19.2 s** latency p50/p95, and approximately **3,700** tokens per question. Record the actual question count, model/prompt configuration, new values, and deltas. Do not compare a different set with these numbers, or claim a baseline comparison from unit tests.

After each run, update this document with the actual attempt time, tested commit/dirty status, status, missing prerequisites, and sanitized report location. Retain executed rows and any real-data screenshots outside tracked files. A failed or pending check remains visible in the final delivery summary.

## Hosted links and targeted comparison follow-up

Hosted result links are now implemented in addition to offline snapshot files. Their API and
browser checks cover explicit confirmation, cross-browser opening without execution, expiry,
policy mismatch, server-only draft creation, bounded storage, persistence, and sanitized failures.
Updated local test totals are recorded after the final run below.

**Targeted real-model comparisons remain PENDING.** Latest actual attempt: **17 October 2026,
19:48:01 Europe/Zurich**. The local dev database, model server, existing model tunnel, and demo API
were all unavailable when checked outside the sandbox. This workspace has no `.env`, the configured
backend is `stub`, and no query model is configured. No services or tunnels were started.

Owner remains Codex/code author, with run-machine coordination by Santiago and the original
submission verification target. The user requested implementation assuming the actual private configuration will be supplied;
`.env.example` and the verification runner now contain explicit comments identifying those required
model/database/API settings and the remaining actual-service run. Code and synthetic tests must not
be presented as a successful real-model run.

Actual targeted command:

```bash
PYTHONPATH=src .venv311/bin/python eval/validate_demo_results.py --execute \
  --case two_cohorts_by_sex --case two_cohorts_diabetes_by_sex --timeout 2 \
  --report eval/reports/comparison-validation.json
```

Evidence: `eval/reports/comparison-validation.json` (ignored, sanitized statuses only). Tested base
commit remains `e67edfd69c837017304da1a26e2ff720e315ed3f` plus the uncommitted implementation. Resolve
this check with the same command plus the authorized LLM-backed `--api-url` and `--browser`, using
the normal timeout. The runner now checks real-model health and exact group-count equality,
independently of mere translation/execution success.

A separate sub-agent reviewed sharing privacy/storage paths. Its process-local draft finding was
resolved by explicitly documenting the default single-worker requirement for publication; a shared
SQLite file alone does not share drafts between workers. Published-link reads persist across restarts.

## Local reproducibility-card verification

Implemented local provenance for released database results, optional operator-provided dataset
identity, a collapsed query/copy UI, exact plain-text record copying, immutable local-history
restoration, and explicit synthetic/legacy/imported/shared states. Snapshot and hosted-share formats
remain unchanged and exclude provenance/SPARQL.

Backend regression tests freeze the clock before mocked execution, exercise repaired SPARQL, preserve
verbatim questions, assert null dataset fields/defaults, and verify withheld/forbidden/error outcomes
contain null provenance. Sharing failure does not remove a valid execution record. Node tests assert
exact LF-separated clipboard records and unchanged snapshot exclusions. Browser tests cover actual
DOM order, collapsed details and keyboard access, escaped content, policy transitions, history reload,
missing version labels, synthetic previews, shared/imported notices, and long queries on desktop/phone
in both themes. All browser evidence uses synthetic fixtures.

The final browser suite passed **64 tests**, including **16 reproducibility tests** across desktop
and phone projects. All **780 Python tests** and **13 Node tests** passed. The local verification
table records the completion time. A phone screenshot was also inspected for wrapping and card layout. Actual model/dev-database verification
remains pending as documented above; these tests do not invent a live execution or dataset version.

## Query-free reproducibility privacy update

17 October 2026, 20:22 CEST: removed query display and query-copy actions from the card and
SPARQL from its copied text. Demo responses now return a null top-level query, query-free
provenance, empty diagnostics, and sanitized execution errors. Saved console history strips older
query fields when loaded; console JSON output also strips query/trace fields. Legacy records
remain readable for their question/time/dataset information. The judged endpoint is unchanged
and must remain outside an end-user deployment boundary when query disclosure is prohibited.

Regression verification: 780 Python tests, 13 Node tests, and 64 Chromium desktop/phone tests
passed, including assertions that legacy query-bearing records cannot disclose queries through
the card or copied record. Actual model/dev-database validation remains pending.

## Follow-up privacy audit

17 October 2026, 20:26 CEST: **788 Python tests, 13 Node tests, and 68 Chromium browser tests passed**.
New regressions cover extra dictionary-field removal, sanitized refusals/errors, public-mode
threshold requirements, developer-route blocking, disabled request archives, developer command
blocking, and full-width interpretation text. The normal judged API remains unchanged.

`DEMO_PUBLIC_MODE=true` is opt-in and was tested with synthetic data. It requires an explicitly
configured positive minimum-group threshold. No actual threshold was selected, live service was
started, or public deployment was changed. Authentication, organizer policy approval, and actual
model/dev-data verification remain deployment prerequisites. See [PRIVACY_REVIEW.md](PRIVACY_REVIEW.md)
for the controls and residual risks; passing these tests does not establish zero disclosure risk.

## Compact reproducibility copy action

The visible metadata card was replaced with a top-right copy icon. It copies the requested preview
format, including the actual displayed count labeled illustrative and not for citation, or the saved
real execution metadata and released result. No query is copied. Table gaps/zero values and
truncation remain explicit. Policy failure/mismatch hides and disables the icon; history uses original
metadata; imported/shared snapshots still have no copyable provenance record.

Final regression run: **788 Python, 14 Node, and 68 desktop/phone browser tests passed**.
Checks include exact copied strings, keyboard operation, hidden metadata, icon placement, policy
transitions, history, snapshot/share exclusions, and light/dark layouts. Desktop and phone synthetic
screenshots were inspected. This UI change does not establish new real-service verification.

## Export icons and visual examples

The three table export actions are now icon-only controls with accessible names, hover titles,
keyboard support, and 44-pixel hit areas. Existing CSV, clipboard, snapshot, and policy-gating
checks still pass. The original starter comparison was expanded into three browser-only synthetic
examples: category bars, wide grouped bars, and long-form grouped bars with a missing pair.

Final local checks: **788 Python tests, 14 Node tests, and 82 Chromium browser tests passed**.
The 14 added browser checks cover all three chart examples in both themes and both viewports,
plus export-icon behavior. They verify exact table cells, numeric labels, proportional bar widths,
missing combinations, no execution requests, and no export controls on unverified previews.
Synthetic chart screenshots and the phone export-icon layout were inspected. Real-data validation
remains pending; these examples neither execute queries nor establish real-model correctness.

## Unified result-action row

17 October 2026, 20:46 CEST: CSV, table-copy, and snapshot icons were aligned immediately beside
the provenance-copy icon at the top-right. All four controls retain their original privacy checks.
Share publication remains a separate action below the table. The desktop screenshot was inspected.

Python: **788 passed**. Node: **14 passed**. The browser run had **81 passing tests** and one
desktop assertion reporting a 44-pixel button as 43.99998 pixels. The size assertion was corrected
to allow subpixel floating-point precision. The affected export/alignment regression was then rerun
on both desktop and phone: **2 passed**, including same-row bounds, no overlap, and keyboard copy.
Latest browser artifacts describe this targeted rerun, rather than a new full 82-test run.

## Single-count display cleanup

Single numeric results now hide their technical hero label and collapse the repeated exact table
and row-count note behind a closed “Show exact value” disclosure. Other result tables and all
exported/copied values are unchanged. Verification: **788 Python tests, 14 Node tests, and 28
focused desktop/phone result/visualization browser tests passed**. The browser regression checks
keyboard expansion/collapse, hidden duplicate values by default, and unchanged chart tables and
exports. Latest browser artifacts refer to these 28 focused checks, not a new full-suite run.

## General live-response shapes and misleading mock fallback

The renderer now locates verified category/measure columns in arbitrary projection order for every
live response. It does not select data by question text, diagnosis, or a preview-case name. The exact
table stays in source order. Reordered wide/long comparisons, numeric category codes, missing
pairs, duplicates, unknown metadata, and mismatched metadata names have regression coverage.
A comparison returning one value receives an explicit notice; separate groups are never invented.
Unknown mock comparison/grouped questions now fail clearly instead of becoming a diabetes-count
fixture. Known authored preview examples remain synthetic and separate from live questions.

Final local verification: **788 Python, 16 Node, and 88 browser tests passed**. Six new browser
checks use synthetic HTTP responses through the normal live `/demo/ask` display path across
multiple questions and shapes. These are integration fixtures, not actual dev-database results.

Actual real-service prerequisite check on 17 October 2026: no private `.env` file, backend `stub`,
and no configured query model. No new live model/database execution was attempted or claimed.
Santiago must load the authorized private connection settings, run an LLM-backed API, open the
console without `?mock=1`, and resolve the reference/NL/browser checks listed above. Clinical
semantic correctness and arbitrary-question translation remain PENDING until verified there.
