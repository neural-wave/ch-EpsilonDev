# Guard and Judge — Santiago handoff

This guide records what Adrika implemented and verified locally, what Santiago still needs to validate on the configured run machine, and which policy choices must wait for organizer decisions. The root [README](../../README.md) is the project-wide source of truth; [docs/DATA.md](../DATA.md) describes the RDF graph structure.

## Handoff status

| Area | Status | Owner / next action |
| --- | --- | --- |
| Guard R1–R5 and shared SPARQL parsing | Implemented and locally verified | Preserve current behavior unless regression evidence supports a bug fix. |
| Judge syntax, prefix, term, and style diagnostics | Implemented and locally verified | Review retry value on fresh run-machine LLM results. |
| Synthetic Guard/Judge regression coverage | Passing locally | Re-run on `guardjudge` with the runbox test command. |
| Real RDF metadata and Virtuoso execution | Not verified | Santiago must profile and execute queries on dev. |
| Fresh LLM evaluation and corpus reconciliation | Not verified | Santiago must run the configured model and inspect local retry logs. |
| Virtuoso-only static diagnostics and performance guidance | Evidence-gated | Reproduce a concrete error or repeatable improvement before adding code. |
| Organizer-dependent policies | Pending | Keep existing behavior until decisions arrive. |

**Code revision described here:** `51d998327ce1e39159e3abcc174ccc5a70f41dfa` on branch `guardjudge`. This handoff rewrite is a documentation-only follow-up to that code revision.

**Access from Adrika's checkout, checked 2026-10-17:** `.env` is absent; localhost ports 8890 (dev) and 8891 (perf) are closed. No real-graph profiling, Virtuoso query execution, or fresh LLM evaluation was performed here. Local tests do not count as real-graph verification.

## What Adrika completed

### Guard

- Fixed R1 classifier wiring in `src/nl2sparql/guard/rules.py` so `_single_patient` uses the supplied `PropertyKinds` instance.
- Restored inverse identifier-path detection. Identifier paths ending in a literal are checked using the supplied classifier; the valid `hasValue` near-miss remains allowed.
- Preserved the current R1–R5 decisions, verdict behavior, reasons, and retry contract: a query violation cannot become compliant, and a request flagged as forbidden is never retried.
- Added or verified synthetic trigger/near-miss coverage across nested queries, graph scopes, `VALUES`, filters, `BIND`, aggregates, grouping, property paths, identity-preserving transforms, `OPTIONAL`, and `UNION`.

### Judge and parser

- Preserved diagnostics for syntax errors, prefix handling, unknown SPHN terms/suggestions, per-pattern ICD/CHOP inference, and `COUNT(DISTINCT ?g)`.
- Added or verified nested-scope unknown-term cases and valid neighbors, plus `DEFINE` and inference `OPTION` parsing behavior.
- Kept the parser database-agnostic. Guard and Judge use the committed local profile snapshots; they do not call Virtuoso.

### Other verified constraints

- API fields, shared interfaces, literal-translation behavior, open-source model requirements, and request-time database independence were left unchanged.
- Organizer-dependent behavior was not introduced for fine-grained grouping, `MIN`/`MAX`, minimum group size, `SERVICE`, or singleton groups.
- Local corpus review found parse errors for #01 and #08 and a discrepancy between committed and current local Guard/Judge acceptance for #07. These are unresolved until a fresh LLM run and retry-log review.

## Local evidence

Latest Guard-specific results recorded for this branch:

```text
PYTHONPATH=src python -m pytest -q tests/test_guard.py -k 'supplied_property_classifier or inverse_identifier_path'
2 passed

PYTHONPATH=src python -m pytest -q tests/test_guard.py
68 passed

PYTHONPATH=src python -m pytest -q tests/test_guard.py tests/test_sparql.py tests/test_agents_and_judge.py tests/test_pipeline_and_api.py
116 passed

PYTHONPATH=src python -m pytest -q
171 passed, 1 existing Starlette/anyio deprecation warning
```

The full-suite result is the latest repository-wide run recorded on the branch. The local stub evaluation recorded 31 questions, 87.1% verdict accuracy, zero forbidden leaks, four allowed requests blocked, seven retries, p50/p95 latency of 12.7/49.9 ms, and zero tokens. This is a local code-path signal only; it is neither the configured LLM evaluation nor a Virtuoso execution result.

## What remains code-wise

There is no known unconditional Guard/Judge implementation item in the current offline checklist. The Judge already has the currently agreed deterministic diagnostics; Guard already covers confirmed R1–R5 policy and the synthetic interaction cases listed above.

Further code changes are conditional:

- Add a static Virtuoso compatibility diagnostic only after Santiago reproduces a specific failure on dev and identifies a query pattern. Pair it with a synthetic valid near-miss.
- Add selective-pattern feedback only after team agreement and repeated performance evidence.
- Add or alter privacy enforcement only after the organizer/team policy decisions below.
- Fix any corpus/evaluation issue only after fresh LLM output and retry logs identify an actionable cause.

## Santiago's run-machine checklist

Run on the configured machine with the `guardjudge` branch. Keep `.env`, raw questions, SPARQL, corpus exports, request logs, full reports, terminology entries, and patient-level query results on that machine. Share sanitized summaries only.

### 1. Check out the branch and rerun the suite

```bash
scripts/runbox.sh status
scripts/runbox.sh test --branch guardjudge
```

Require the full suite to pass. Record the tested commit and summary. If it fails, share sanitized failing test names and traceback only. Do not claim this test run verifies RDF freshness or Virtuoso compatibility.

### 2. Compare committed snapshots with the dev RDF profile

Generate a candidate profile in a temporary directory so the committed snapshots stay untouched:

```bash
repo_root="$PWD"
worktree="$(dirname "$repo_root")/nl2sparql-worktrees/guardjudge"
profile_dir="$(mktemp -d)"

(cd "$worktree" && \
  PYTHONPATH="$worktree/src" "$repo_root/.venv/bin/python" \
  scripts/profile_data.py \
  --endpoint http://localhost:8890/sparql \
  --data-dir "$profile_dir")

(cd "$worktree" && \
  PYTHONPATH="$worktree/src" "$repo_root/.venv/bin/python" \
  scripts/compare_data_profile.py \
  --profile-dir "$profile_dir")
```

Comparator outcomes: exit code `0` means snapshots match; `1` means profile drift; `2` means a file is missing, malformed, or empty. Check the configured patient and ontology graph IRIs against [docs/DATA.md](../DATA.md). Review aggregate additions/removals/changes locally. Do not paste raw profile entries or diffs into shared reports. Refresh snapshots only when the measured dev graph supports it, then rerun focused and full tests. `factory.py` snapshot validation is owned by teammate A; coordinate before changing it.

### 3. Run fresh LLM evaluation with dev execution

```bash
scripts/runbox.sh eval --execute --branch guardjudge
```

Review the complete report locally. Compare against the historical root README §11 baseline: 90% LLM verdict accuracy, zero forbidden requests marked compliant, p50/p95 latency of 2.8/16.6 seconds, and about 3,000 tokens per request. Record sanitized counts for false positives, false negatives, forbidden leaks, retries, empty results, execution errors, latency, and tokens. Do not share raw report rows, request text, or query text.

### 4. Reconcile the corpus and retry behavior

Export fresh corpus data to a protected temporary path, not over the committed corpus by default:

```bash
corpus_dir="$(mktemp -d)"
python scripts/export_guard_corpus.py --out "$corpus_dir/guard_corpus.jsonl"
```

Review #01, #07, and #08 against the new run and inspect local retry logs. Determine whether the failure is from generation, Judge feedback, or the shared retry. Do not infer the cause from the old corpus alone. Keep exported corpus and logs private.

### 5. Reproduce Virtuoso errors before proposing static checks

- Reproduce the documented #05 and #10 failures on dev; capture the error category and execution stage locally.
- Investigate the reported `NULL` and nested-`GRAPH` patterns.
- Only propose a database-agnostic static diagnostic when the failure is reproducible and the syntax pattern is identifiable. Add a synthetic failing case and a valid near-miss, then rerun all local tests and dev evaluation.
- Never add a Virtuoso call to request-time Guard/Judge code.

### 6. Resolve policy and performance gates

Keep existing behavior until the organizers/team answer:

1. Is fine-grained grouping, such as grouping by exact birth date, compliant?
2. Should `MIN`/`MAX` of values or dates be restricted when they can single out a record or patient?
3. Is minimum group size required? If so, what threshold applies, including groups reduced to one by `HAVING`?
4. Is `SERVICE` allowed?

Before adding selective-pattern ordering feedback, get team agreement and measure repeated isolated timings on the perf graph. This repository does not currently provide a dedicated ten-run Guard/Judge timing harness. Do not add automatic query rewrites until synthetic equivalence cases and dev result comparisons show the rewrite preserves query meaning, including duplicates, grouping, optional patterns, and nested queries.

## What Santiago should report

Update this guide with the tested branch commit and only sanitized summaries:

- profile match/drift and aggregate counts;
- dev query execution success, empty-result, and error counts by example ID where safe;
- fresh LLM accuracy, false positive/negative, forbidden leak, retry, latency, and token metrics versus baseline;
- whether #01/#07/#08 discrepancies were explained;
- reproduced Virtuoso error categories and whether any static pattern is justified;
- perf medians/spread and any follow-up code that evidence supports;
- organizer decisions and remaining blockers.

Do not add raw patient results, questions, SPARQL, corpus exports, terminology content, credentials, `.env`, GPU host details, or raw run-machine reports to Git or this guide.

## Invariants

- Database access stays in maintenance/evaluation tooling, outside `POST /nl-to-sparql`.
- Forbidden intent is never retried; a query violation never becomes compliant.
- Preserve fixed API fields, shared interfaces, and literal translation.
- Keep organizer-dependent policy unchanged until decisions are recorded.
