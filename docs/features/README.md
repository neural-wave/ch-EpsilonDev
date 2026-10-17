# Santiago handoff: demo results, privacy, sharing, and reproducibility

Prepared for **Santiago Figueroa Luchetti** by Adrika's implementation workflow with Codex. Status date: **17 October 2026**, Europe/Zurich. Submission target in the team README: **18 October 2026 at 11:00 Europe/Zurich**.

The implementation is complete for local code delivery and has passed automated local checks. **It is not yet verified against the actual model and dev database.** Santiago's remaining work is to configure the existing services, confirm dataset identity and the organizer's privacy policy, run real-data validation, and record the results before submission.

This handoff covers the result console, privacy controls, comparison rendering, and query-free reproducibility copying. It does not replace the [team master README](../../README.md), the guard/judge handoff, or the agents/data guides. No private credentials or patient results belong in this document.

> **Current status:** Local implementation and regression checks pass. Real-data translation, comparison accuracy, and deployment verification remain **PENDING**. No private `.env` or query model is configured in this checkout. The latest full run passed **788 Python, 16 Node, and 88 browser tests** on **17 October 2026 at 20:59 CEST**.

## 1. What has been implemented

| Feature | Implemented behavior | What still needs Santiago |
|---|---|---|
| Automatic visuals | Verified numeric aggregates become a large number; category counts become bars; supported wide or long-form comparisons become grouped bars. Verified roles work across arbitrary aliases and projection orders. Other shapes remain tables; a single-count table is collapsed under **Show exact value**. | Check the actual model's returned shapes and confirm table/chart values against reference rows. |
| Exact tables | Dictionary rows follow backend column order. Missing cells remain empty. Pagination is 50 rows; response cap is 200 rows with an explicit truncation flag computed before slicing. Single counts omit the technical hero label, such as `countResult`. | Confirm with the actual dev database. |
| Privacy badge and policy loading | Persistent aggregate-only badge, threshold/disabled notice, retry on policy failure. Loading or failed policy hides sensitive result content and disables imports/exports. | Approve the policy wording and confirm the intended threshold with organizers. |
| Small-group withholding | Optional, demo-only, disabled by default. All executed rows are checked before truncation. Unsupported semantics, invalid counts, or any count below threshold withhold the entire result. | Decide whether to enable it and run both enabled/disabled validation. |
| CSV and clipboard | All returned rows can be downloaded/copied. CSV, table copy, and snapshot download use icons beside the provenance-copy icon in one top-right row, with hover titles and keyboard access. CSV quoting and textual spreadsheet-formula protection are included. | Check a real released table and the research workflow. |
| Compare two groups | One question and one existing request; renderer supports two measures or category/cohort/count. Missing combinations are gaps, duplicates remain a table. No fabricated counts or automatic second query. A single returned value shows a comparison-chart limitation notice; unsupported mock comparisons no longer reuse the diabetes count. | Verify actual NL translation and exact cohort counts; rendering support does not prove model translation quality. |
| Offline snapshots | Browser-only version-1 JSON download/import with strict structure/size checks. Imports are labeled unverified and cannot be re-exported until explicitly re-asked. | No extra implementation required; review the limitations below. |
| Hosted shareable links | Explicit confirmation publishes a server-staged released result. Another browser can open it without running a query. Links expire after seven days by default; expired or policy-mismatched results are blocked. | Confirm deployment/storage settings and test cross-browser access on the actual demo origin. |
| Local reproducibility copy | A corner icon copies the original question, released result, actual execution timestamp, and dataset identity. Details are hidden from the page. No SPARQL is displayed, copied, returned by the demo, or retained in new/reopened history. Preview records are explicitly illustrative and not for citation. | Fill dataset identity and verify the copied record from a real released execution. |
| Synthetic chart examples | Three clickable start-page examples demonstrate category bars, wide grouped bars, and long-form comparisons with a missing pair. They never execute a query and have no export controls. | Use them for UI review only; they do not validate the model. |
| Public-console mode | Opt-in `DEMO_PUBLIC_MODE=true` blocks developer/judged/docs routes, disables request archives, and requires an explicitly configured positive minimum-group threshold. | Confirm organizer policy and deploy behind authorized access/TLS. Keep the jury API separate. |
| Regression coverage and documentation | Python, Node, and Chromium desktop/phone checks; policy transitions, history, export, sharing, provenance, keyboard access, themes, and overflow covered with synthetic fixtures. | Re-run on the final checkout and complete live checks. |

## 2. What was deliberately preserved

- `/nl-to-sparql` remains database-agnostic. Database execution and result suppression are limited to the optional `/demo/ask` dev path and explicit evaluation scripts.
- Shared models, agent interfaces, prompts, and guard verdict behavior were not changed by this result-presentation/provenance work.
- Forbidden demo requests never execute. The demo never returns generated queries, query-bearing diagnostics, model suggestions, or raw database/pipeline errors. Refusals use a sanitized aggregate-only explanation.
- Developer commands do not retrieve internal context, reports, model configuration, or query diagnostics through the user console.
- `/nl-to-sparql` still returns queries under its fixed contract on the normal developer/jury server; it is blocked in the opt-in public-console process.
- Suppression is separate from compliance. `withheld` means the database executed successfully but the demo cannot release its result; it is not a database-disconnection error.
- No GPU administration or infrastructure provisioning was performed for these changes.

## 3. Files to review

| File | Purpose |
|---|---|
| [config.py](../../src/nl2sparql/config.py) | Public-console switch, minimum-group policy, share settings, and dataset identity parsing. |
| [api.py](../../src/nl2sparql/api.py) | `/demo/config`, `/demo/ask`, executed-result processing, provenance attachment, and sharing routes. |
| [demo_results.py](../../src/nl2sparql/demo_results.py) | Query-derived column semantics, conservative suppression, and pre-cap truncation. |
| [demo_shares.py](../../src/nl2sparql/demo_shares.py) | Server-created share drafts and published snapshot persistence. |
| [policy.js](../../frontend/js/policy.js) | Loading/available/failed policy controller and retry/subscription behavior. |
| [result.js](../../frontend/js/result.js) | Shared charts/table renderer, exports, snapshot validation, query-free provenance copying, and policy gating. |
| [api.js](../../frontend/js/api.js) | Live/mock clients and rejection of unsupported mock comparison/grouped requests. |
| [cli.js](../../frontend/cli/cli.js) | Live/mock requests, original input preservation, history, imports, sharing. |
| [cli.css](../../frontend/cli/cli.css) | Responsive console, aligned icon toolbar, and chart styling. |
| [.env.example](../../.env.example) | Public configuration template; actual values go only in private `.env`. |
| [validate_demo_results.py](../../eval/validate_demo_results.py) | Explicit real-service reference/NL/browser verification runner. |
| [live-shapes.spec.js](../../tests/browser/live-shapes.spec.js) | General live-response rendering, reordered columns, and misleading mock fallback regressions. |
| [visualizations.spec.js](../../tests/browser/visualizations.spec.js) | Synthetic charts, table/bar equality, icon accessibility, and row alignment. |
| [PRIVACY_REVIEW.md](../PRIVACY_REVIEW.md) | Deployment boundary, controls, and remaining privacy limitations. |
| [FRONTEND.md](../FRONTEND.md) | Full response/UI/sharing/provenance contracts. |
| [RESULTS_VALIDATION.md](../RESULTS_VALIDATION.md) | Actual evidence, pending checks, ownership, and required reporting. |

## 4. Configuration Santiago must supply

Use the existing authorized dev services. The latest prerequisite check found no private `.env`, backend `stub`, and no configured query model. Prior service attempts are recorded separately in the validation record. Actual model/database execution cannot be certified from the current local setup. Do not overwrite an existing private configuration with the example file.

If `.env` does not exist, copy `.env.example` once. Set the following in the private file:

```dotenv
NL2SPARQL_BACKEND=llm
LLM_BASE_URL=<existing authorized model API base URL>
LLM_API_KEY=<credential if required by that service>
LLM_QUERY_MODEL=<actual running open-source model name>
# Set LLM_INTENT_MODEL if it differs; otherwise it inherits the query model.
VIRTUOSO_ENDPOINT=<existing dev SPARQL endpoint>

# Required for Qwen3.x according to the team guide:
LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'

# Development/jury process; not the public end-user deployment:
DEMO_PUBLIC_MODE=false
# Keep disabled until the organizer/team policy is agreed:
DEMO_MIN_GROUP_SIZE=disabled

# Replace with the real, operator-verified dataset identity:
DEMO_DATASET_NAME=<actual dataset name>
DEMO_DATASET_VERSION=<stable release or locally assigned immutable version>
DEMO_DATASET_DESCRIPTION=<accurate description of this dataset>

DEMO_SHARE_TTL_SECONDS=604800
# Optional; default is logs/shared_results.sqlite3:
# DEMO_SHARE_DB_PATH=<private writable persistent path>
```

These are placeholders, not a runnable credential file. Do not infer dataset version from the endpoint, assume the patient count, or label a dataset synthetic without confirming it. Blank dataset fields become null and copy as **Not configured**; a missing version marks the copied reproducibility record incomplete. A version label does not archive the dataset or guarantee its future availability.

`DEMO_MIN_GROUP_SIZE` accepts unset/blank/`disabled` or a positive integer such as `5`. Invalid values fail startup. Dataset variables are trimmed, with blank values becoming null. Dataset identity is sent per executed answer; `/demo/config` continues to contain only policy threshold, row limit, and policy version.

Export settings into the shell **before starting the API and before running evaluations**; simply creating `.env` is insufficient:

```bash
source .venv/bin/activate   # Or .venv311/bin/activate if that is your environment.
set -a
source .env
set +a
PYTHONPATH=src python -m nl2sparql --host 127.0.0.1 --port 8080
```

Use the default single-worker process for the complete ask-to-share flow. Published shares are persisted in SQLite, but unpublished draft tokens are process-local; a shared SQLite path alone does not make draft publication work across workers. This command does not start the model, database, tunnel, or GPU.

### Separate public-console deployment

After organizers approve the threshold, set `DEMO_PUBLIC_MODE=true` and the approved positive
`DEMO_MIN_GROUP_SIZE` in the separate end-user process. Public mode refuses startup with a disabled
or invalid threshold. It allows only `/`, `/health`, `/demo/*`, and `/console/*`; developer,
judged, and documentation endpoints receive HTTP 404. Structured pipeline request archives are
disabled. Default development mode remains unchanged and is not the public privacy boundary.

Do not enable public mode on the jury's API process: it intentionally blocks `/nl-to-sparql`.
Configure authentication/authorized access, TLS, protected SQLite storage, and infrastructure log
retention separately. This switch is not an authentication system or a compliance certificate.
Read [PRIVACY_REVIEW.md](../PRIVACY_REVIEW.md) before deployment.

## 5. Santiago's completion checklist, in order

### A. Identify the actual checkout and configure services

- [ ] Obtain the implementation branch/commit containing these files. This handoff does not mean the changes were committed, pushed, or merged.
- [ ] Record `git rev-parse HEAD` and `git status --short`; tests of a dirty tree cannot be attributed to the base commit alone.
- [ ] Use the existing authorized model and dev database, load private environment settings, and start the API as above.
- [ ] Confirm `GET /health` reports the LLM backend, not `stub`, and `GET /demo/config` reports the intended policy.
- [ ] Set and verify dataset name/version/description. Confirm minimum-group policy with the organizers; do not enable an arbitrary threshold as though it were approved.

### B. Re-run local suites on that checkout

Python 3.11+ and Node/npm are required. After installing Python requirements into the active environment:

```bash
PYTHONPATH=src python -m pytest -q
node --test tests/frontend/*.test.cjs
npm ci --prefix tests/browser
npm exec --prefix tests/browser -- playwright install chromium
BROWSER_TEST_PYTHON="$(command -v python)" npm test --prefix tests/browser
```

The browser harness uses its own stub server at `127.0.0.1:18081` and refuses to reuse an existing server. Keep that port free. It uses deterministic synthetic route fixtures; passing it is not live database verification. JUnit, HTML reports, screenshots, traces, and downloads go under ignored `tests/browser/artifacts/`.

Last completed full local verification: **788 Python tests, 16 Node tests, and 88 browser tests
passed**, recorded on **17 October 2026 at 20:59 CEST**. Tests cover desktop and 390×844 phone
layouts, light/dark themes, keyboard controls, exports, history, privacy states, aliases/column
permutations, chart/table equality, missing pairs, and mock fallback rejection. One existing
Starlette/AnyIO deprecation warning remains. Browser responses are synthetic fixtures, including
those intercepted through the live UI route; they are not actual model/database validation.
These figures describe the recorded implementation checkout, not a new run on Santiago's machine.

### C. Complete real-data and comparison verification

With the authorized API already running on port 8080 and the same environment loaded in another shell:

```bash
PYTHONPATH=src python eval/validate_demo_results.py --execute \
  --api-url http://127.0.0.1:8080 --browser

PYTHONPATH=src python eval/validate_demo_results.py --execute \
  --api-url http://127.0.0.1:8080 --browser \
  --case two_cohorts_by_sex --case two_cohorts_diabetes_by_sex \
  --report eval/reports/comparison-validation.json
```

The runner checks a distinct-patient cholera count, cholera by sex, and cholera/no-cholera and diabetes/no-diabetes comparisons by sex. It compares complete reference rows with processed released cells, semantic metadata, truncation, suppression under disabled/threshold-5 policies, and actual browser table/chart output. The API checks use the existing model separately; targeted comparisons also compare cohort counts with reference values. A successful HTTP response or executable SPARQL alone is insufficient.

- [ ] Require matching counts and category/cohort labels, with no fabricated zeros or summed duplicate pairs.
- [ ] Confirm the real comparison activates the expected chart. If data cannot exercise the shape, keep that coverage pending rather than claiming it passed.
- [ ] Investigate `FAIL`; retain missing prerequisites as `PENDING`. Runner exit codes: `0` pass, `1` failure, `2` pending/invalid invocation.
- [ ] Store raw results and real-data screenshots outside tracked files. Sanitized status reports may stay in ignored `eval/reports/`.

### D. Check ordinary live questions and the final UI

Open `http://127.0.0.1:8080/console/cli/` **without `?mock=1`**. Check that `/health` reports
`llm`; `stub` merely retrieves a nearby example and does not prove arbitrary-question translation.

Try ordinary questions, rather than the synthetic starter buttons:

- “How many patients have a billed diagnosis of type 2 diabetes?”
- “How many patients have a billed diagnosis of cholera, grouped by sex?”
- “How many patients have a billed diagnosis of type 2 diabetes compared with those without it, grouped by age group?”

- [ ] Verify the simple count against a known reference. It should show the large number with
  **Show exact value** closed; technical labels and the duplicate table should be hidden initially.
- [ ] Verify the comparison query actually includes both cohorts and the requested grouping.
  A single count is not a comparison answer. Confirm the age definition and bucket boundaries;
  do not assume the synthetic preview's age bands define the real study.
- [ ] Confirm charts use returned values and verified metadata for each condition. Category/measure
  columns may be reordered; original table column order must remain unchanged. Unsupported or
  ambiguous shapes stay tables, and missing groups remain gaps rather than invented zeros.
- [ ] Confirm the single-cell comparison warning appears when the model returns only one value.
  Do not mark comparison translation passed just because a query executes or a chart is visible.
- [ ] Check all four icons are side by side at the top-right: CSV, table copy, snapshot download,
  and reproducibility copy. Hover labels and keyboard activation must work on desktop/phone.
- [ ] Copy provenance. It should contain the original question, released result, actual UTC time,
  and configured dataset fields, with no SPARQL or query-disclosure section on the page.
- [ ] Reload/open history and copy again. The original timestamp, dataset, and result must survive;
  current configuration must not rewrite an old record. Legacy query fields must be stripped.
- [ ] Confirm forbidden, withheld, failed, and unexecuted outcomes have no provenance-copy action.
  Policy loading/failure/mismatch must hide sensitive content and disable actions.
- [ ] Download a snapshot and publish a share link from a released result. Both saved formats must
  exclude `reproducibility`, query fields, diagnostics, and connection details. Review the question
  for confidential wording before publication. A second browser must open the same table without
  issuing another ask; expiry and policy mismatch must block access.
- [ ] Confirm imported snapshots and hosted links have no complete provenance record or fabricated
  execution timestamp. Unverified imports must not gain export/provenance controls.

For UI-only checks without actual data, open `/console/cli/?mock=1`, click the logo, and select one
of the three **Preview example** chart buttons. These show synthetic bar/grouped-bar shapes and
make no database/model request. Arbitrary comparison/grouped text in preview mode now produces a
clear unsupported message, instead of the unrelated 412-patient diabetes fixture.

Preview provenance copies use this format; the number is explicitly illustrative:

```text
Preview mode (no database)

- Source: Synthetic preview, not for citation
- Question: how many people have type 2 diabetes
- Result: 412 (illustrative number)
- Executed at: Not applicable
- Dataset: None, demonstration only
```

### E. Run the pipeline evaluation and record the comparison

```bash
PYTHONPATH=src python eval/run_eval.py --execute
```

For the matching **31-question shared set**, the README baseline is **90.3% verdict accuracy**, **0 forbidden requests marked compliant**, **3.1 s p50 / 19.2 s p95**, and about **3,700 tokens per request**. Record the actual question count, model/prompt configuration, accuracy, forbidden-marked-compliant count, latency, tokens, and differences. Do not compare a different evaluation set to these numbers. Do not use the local test totals as evidence of model accuracy.

- [ ] Update [RESULTS_VALIDATION.md](../RESULTS_VALIDATION.md) with actual timestamp, tested commit/dirty status, command, `PASS`/`FAIL`/`PENDING`, sanitized outcome, missing prerequisites, and evidence location for each external check.
- [ ] Keep remaining pending/failed checks visible in the report/video and handoff. **Code delivery is complete; fully verified delivery requires the real-service checks to pass.**
- [ ] Review and integrate through the team's normal branch/PR process; do not commit `.env`, assets, restricted terminology, patient data, local share databases, or test artifacts.

## 6. Important limitations to explain in the demo/report

**Suppression is conservative and optional.** It verifies direct distinct-patient counts from supported query semantics; averages, derived measures, unknown projections, malformed counts, and unsupported shapes may be withheld when enabled. A total and small subgroup are withheld together, but repeated-query differencing across separate questions is outside this feature. The guard is not proof that every aggregate is safe under every privacy policy.

**Provenance is query-free and local.** The demo response and local history retain question, execution time, and dataset identity; copied records also include the released result. Generated queries are not returned, displayed, copied, or retained in reopened history. Offline snapshots and hosted links intentionally exclude provenance. Preview copies are explicitly illustrative, not for citation, with no invented timestamp. Older history cannot be retroactively reconstructed.

**A share is an unlisted link, not an authenticated research repository.** Anyone with it may access its released question and aggregate table until expiry. The question is included, so the publish confirmation must be reviewed for sensitive wording. Do not describe it as permanent, access-controlled archival storage. Policy gating hides browser content/actions; it cannot retract already delivered data or copies.

**Charts do not repair translation.** Rendering is general across supported metadata/row shapes, not hard-coded to a diagnosis or example. The model must still generate the correct cohorts and grouping. A correct-looking chart can display a semantically wrong answer; query execution alone is insufficient. Conditions with little data, unsupported aggregates, unknown metadata, duplicate pairs, or active suppression may legitimately produce a table, empty result, or withholding.

**No zero-risk claim.** The default development configuration leaves minimum-group suppression disabled pending policy approval. Public mode requires an explicit threshold, but neither the guard nor that threshold proves anonymity. Free-text questions, dataset descriptions, local browser history, downloaded files, and unlisted links need an agreed handling policy. Clear history on shared devices; repeated-query inference is not prevented by this feature.

## 7. Definition of done for Santiago

The code/UI work is already implemented and locally tested. Santiago can mark the handoff fully verified when the final checkout passes all local suites, the real model/dev database checks pass, dataset identity and the suppression decision are confirmed, sharing is checked on the deployment origin, and the validation record contains actual evidence. Until then, use **implemented; live verification pending** rather than **fully verified**.

See the [demo privacy review and public-console deployment requirements](../PRIVACY_REVIEW.md) before exposing real-data results to end users.
