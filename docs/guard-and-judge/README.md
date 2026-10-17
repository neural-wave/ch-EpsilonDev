# Guard and Judge — Santiago handoff

This guide records what is implemented and verified locally, what Santiago needs to validate on the configured run machine, and which policy decisions must come from the organizers. The root [README](../../README.md) remains the project-wide source of truth.

## Status at a glance

| Area | Status | What the status means |
|---|---|---|
| Guard and Judge local implementation | **Done for current offline scope** | Synthetic tests pass, including the R1 classifier wiring and inverse-path regressions. |
| Local verification | **Passed** | Guard: 68 passed; focused Guard/Judge/SPARQL/pipeline suite: 116 passed; full suite: 133 passed. |
| Organizer-dependent policy | **Pending** | Do not change behavior for fine-grained grouping, `MIN`/`MAX`, group size, `SERVICE`, or singleton groups until decisions arrive. |
| RDF snapshot freshness and Virtuoso execution | **Pending Santiago** | This checkout has no `.env`; dev/performance endpoints are unavailable here. Local tests do not establish real-graph compatibility. |
| Fresh LLM evaluation and retry review | **Pending Santiago** | Needs the configured model/run machine and updated evaluation logs. |

## What is implemented

The request pipeline is database-agnostic. It parses SPARQL and uses the checked-in `data/schema_cards.json` and `data/property_kinds.json`; Guard and Judge do not query Virtuoso. Database calls belong only in profiling and evaluation scripts or the demo's optional development check.

The **Judge** adds missing prefixes, parses SPARQL (including the supported Virtuoso extensions), checks syntax and SPHN terms against local schema cards, and reports the current style diagnostics for typed code-pattern inference and patient counts using `COUNT(DISTINCT ?g)`. Its regression tests cover nested unknown terms and valid near-misses, syntax and prefix handling, inference, graph counts, and extension parsing.

The **Guard** checks the final query and intent flags against R1–R5:

| Rule | Current check |
|---|---|
| R1 | Reject identifiable patient targeting, identifier literal lookups, and filters that compare identifier values. |
| R2 | Reject unaggregated rows and patient- or record-level results. |
| R3 | Reject unrestricted whole-graph matching. |
| R4 | Allow read-only `SELECT` queries only. |
| R5 | Reject patient or record identifiers exposed directly, through identity-preserving expressions, or through sensitive aggregates. |

The Guard uses the supplied `PropertyKinds` classifier for identifier literal lookups and handles inverse identifier paths. The synthetic tests cover nested queries, graph scopes, `VALUES`, filters, `BIND`, aggregates, grouping, property paths, identity transforms, `OPTIONAL`, and `UNION`, with valid neighboring cases. A forbidden intent is never retried; query violations remain non-compliant.

## Work completed locally

- Fixed R1 classifier wiring and inverse identifier-path detection in `src/nl2sparql/guard/rules.py`.
- Preserved synthetic passing/failing examples in `tests/fixtures/guard_judge_baseline.json`.
- Added or verified trigger/near-miss coverage for R1–R5 and the current Judge/parser diagnostics. No organizer-dependent policy was added.
- Verified metadata startup validation, API contract tests, literal-translation behavior, and the absence of request-time database calls.
- Reviewed local corpus outcomes without copying raw questions or query text here: #01/#08 have parse errors; #07's committed status differs from current local Guard/Judge acceptance. A fresh LLM run is needed to resolve the discrepancy.

### Local verification

Run locally from the repository root with Python 3.11 or newer:

```bash
PYTHONPATH=src python -m pytest -q tests/test_guard.py -k 'supplied_property_classifier or inverse_identifier_path'
PYTHONPATH=src python -m pytest -q tests/test_guard.py
PYTHONPATH=src python -m pytest -q tests/test_guard.py tests/test_sparql.py tests/test_agents_and_judge.py tests/test_pipeline_and_api.py
PYTHONPATH=src python -m pytest -q
```

Latest results: the two R1 regressions passed; Guard tests **68 passed**; focused tests **116 passed**; full suite **133 passed**. The focused and full runs each reported one existing Starlette/anyio deprecation warning. `PYTHONPATH=src` is used because this checkout's tests require the source tree on the import path.

The latest local stub evaluation reported 31 questions, 87.1% verdict accuracy, zero forbidden leaks, four allowed requests blocked, 7 retries, p50/p95 latency of 12.7/49.9 ms, and zero tokens. This is a local regression signal only. It is not the LLM evaluation and does not execute queries against the real graph.

## Santiago's run-machine checklist

Push the fixed branch so it is available to Santiago. `scripts/runbox.sh` checks it out into a separate worktree and sets `PYTHONPATH` to that worktree's source.

### 1. Verify the branch and test suite

```bash
scripts/runbox.sh test --branch guard/your-branch-name
```

Require the full suite to pass. Record the commit and test summary. If it fails, keep the run-machine validation open and send back the failing test names and sanitized traceback only.

### 2. Compare local metadata snapshots with the dev graph

Profile to a temporary directory so the committed snapshots are not overwritten:

```bash
profile_dir="$(mktemp -d)"
python scripts/profile_data.py --endpoint http://localhost:8890/sparql --data-dir "$profile_dir"
diff -u data/property_kinds.json "$profile_dir/property_kinds.json"
diff -u data/schema_cards.json "$profile_dir/schema_cards.json"
```

Confirm that the dev graph and ontology IRIs match the documented configuration, and that profiling returns non-empty classes and properties. Summarize whether each snapshot matches and explain any material differences. Refresh committed snapshots only when the profile justifies the change; then rerun the branch test suite. Coordinate with the owner of `factory.py` before changing snapshot validation.

### 3. Run fresh LLM evaluation and dev execution

```bash
scripts/runbox.sh eval --execute --branch guard/your-branch-name
```

Compare verdict accuracy, false positives, false negatives, forbidden leaks, retries, latency, and tokens against the historical root README §11 baseline: LLM accuracy 90%, zero forbidden leaks, p50/p95 latency 2.8/16.6 seconds, and about 3,000 tokens per request. Record compliant-query execution status, empty-result status, and timing by example. Do not copy patient-level results into the report.

### 4. Review corpus discrepancies and compatibility errors

- Regenerate the corpus from fresh run-machine logs to a protected temporary path, not the committed corpus file by default:

  ```bash
  corpus_dir="$(mktemp -d)"
  python scripts/export_guard_corpus.py --out "$corpus_dir/guard_corpus.jsonl"
  ```
- Review the new outcomes for #01, #07, and #08 and inspect the run-machine retry logs to determine whether Judge feedback or the shared retry is actionable.
- Reproduce the documented #05 and #10 Virtuoso failures. Investigate the reported `NULL` and nested-`GRAPH` error categories. Add a static diagnostic only for a reproduced, identifiable query pattern, and pair it with a synthetic valid near-miss.
- Keep raw corpus exports, request logs, full evaluation reports, question text, and query text on the run machine. Share only sanitized outcomes, error categories, retry counts, and timings.

### 5. Decide ordering and performance guidance

Get team agreement before adding selective-pattern ordering feedback. If approved, use repeated isolated timings on the performance graph (`docker start virtuoso-perf` if its container is stopped). The current repo does not include a dedicated ten-run timing harness; coordinate with the evaluation owner or provide a controlled run script before reporting results. Add guidance only for repeatable gains. Do not add an automatic rewrite until regression tests and dev-graph result comparisons demonstrate equivalent results, including duplicates, grouping, optional patterns, and nested queries.

## Organizer decisions required

Keep current behavior until the organizers or team policy owners answer these questions:

1. Should aggregation grouped by a fine-grained attribute be non-compliant?
2. Should `MIN` or `MAX` be restricted when the result could describe or single out one patient or record?
3. Is a minimum group size required? If so, what threshold applies and what should happen below it, including a singleton produced by a `HAVING` condition?
4. Are `SERVICE` clauses allowed?

No enforcement for these cases is included in the current local work.

## Constraints and report format

- Keep database access outside the request-time API. Do not change shared interfaces or API response fields. Teammate A owns `factory.py` snapshot validation; coordinate before changing it.
- Preserve literal translation, no-retry behavior for forbidden requests, and the rule that a query violation cannot become compliant.
- Use only synthetic fixtures in unit tests. Do not add patient data, restricted terminology content or derived assets, secrets, GPU host details, or raw run-machine logs to this guide or Git.
- After run-machine checks, update the status table above and the checklist with the commit tested, snapshot comparison result, sanitized execution outcomes, evaluation metrics, and remaining blockers. Passing local tests must not be described as real-graph verification.
