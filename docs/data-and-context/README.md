# Data and Context — refinement checklist

This guide separates work that can be developed and verified locally from checks that need the configured RDF graphs. The root [README](../../README.md) remains the project-wide source of truth; [docs/DATA.md](../DATA.md) describes the graph layout and data conventions.

## Status at a glance

| Area | Status | Evidence / dependency |
| --- | --- | --- |
| Context request path | **Implemented** | `ContextBuilder` combines retrieved examples, schema cards, and terminology candidates using local files; no database access is performed for a request. |
| Example retrieval | **Refined locally; broader quality review open** | Boundary handling, normalized exact exclusion, deterministic ordering, and a small synthetic benchmark are covered. |
| Schema context | **Implemented; freshness pending** | Cards are loaded from `data/schema_cards.json`; the production factory validates the RDF profile snapshots. Freshness must be compared with the dev graph. |
| Terminology context | **Code implemented; build/validation pending** | `scripts/build_terminology_index.py` and `TerminologyIndex` exist. The generated index is derived from restricted terminology and must stay local in ignored `assets/`. |
| Offline test coverage | **Expanded; further cases open** | Context and pipeline tests cover retrieval boundaries, malformed metadata, deterministic ties, and a no-Virtuoso context path. |
| Real-graph checks | **Pending Santiago/run machine** | Profiling and terminology-index generation need the dev Virtuoso endpoint. This checkout has no `.env` or generated `assets/` index; no graph checks were run. |
| Perf evaluation | **Pending measurement** | Any retrieval or context-size change must be compared in the end-to-end evaluation; graph query timings belong to the perf workflow. |

## What is already implemented

- `ExampleBank` loads example question/query pairs and ranks them with the local BM25 implementation in `bm25.py`.
- `SchemaCards` selects cards for named classes, renders properties and counts, and offers close matches for data terms.
- `TerminologyIndex` finds candidates from locally generated code entries by explicit code or label-token overlap.
- `ContextBuilder` assembles those three context sources, with configurable top-k sizes and an option to exclude an exact question match.
- `scripts/import_examples.py`, `scripts/profile_data.py`, and `scripts/build_terminology_index.py` provide the offline build/import paths.
- The API request path reads the local snapshots/index. It does not query Virtuoso; keep database calls confined to profiling and evaluation tools.

## Completed locally

- [x] Read and map the current context code, data-generation scripts, committed metadata files, and `tests/test_context.py`.
- [x] Confirmed that BM25 example lookup, schema-card selection, terminology candidate lookup, and combined context construction are implemented.
- [x] Confirmed the production factory fails clearly when required schema/property profile snapshots are missing or invalid. Coordinate with teammate A before changing this factory-owned validation.
- [x] Confirmed the generated terminology index is intentionally stored under ignored `assets/` and must not be committed or copied into shared reports.
- [x] Corrected nonpositive `k`, empty-query handling, normalized exact-example exclusion, and deterministic tie ordering across retrieval components.
- [x] Hardened example, schema-card, and terminology-file loading with path-specific errors; pipeline construction now wraps example/index loader failures with the file path.
- [x] Added an isolated test confirming context construction works with `VIRTUOSO_ENDPOINT` unset and does not import the Virtuoso client.
- [x] Kept BM25 defaults after the synthetic parameter benchmark showed no accuracy improvement.

### Local verification evidence

**Tested commit:** uncommitted working tree based on `86cd0443e949d7e84c98abe28b35d0f5a7ad3ae3`. The SHA identifies the base, not the modified tree.

```text
PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_context.py
36 passed

PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_context.py tests/test_pipeline_and_api.py
56 passed, 1 existing Starlette/anyio deprecation warning

PYTHONPATH=src .venv311/bin/python -m pytest -q
163 passed, 1 existing Starlette/anyio deprecation warning
```

`PYTHONPATH=src` was needed in this checkout. The synthetic retrieval benchmark used six fabricated cases. Baseline and both candidate BM25 settings scored **83.3% top-1** and **100% top-3**; p50 retrieval latency was **0.0038 ms**, **0.0037 ms**, and **0.0036 ms** respectively. The candidates did not meet the quality threshold, so defaults are unchanged. These measurements are a small local signal, not an LLM or real-graph evaluation.

## Local work — no RDF graph required

Complete these tasks with synthetic fixtures and the checked-in, permitted snapshots. Do not copy restricted terminology content into tests.

### 1. Strengthen deterministic unit coverage

- [x] Cover retrieval empty/stopword-only inputs, nonpositive and oversized `k`, normalized exact-example exclusion, stable example order, alphabetical schema ties, terminology source-order ties, and repeated context output.
- [x] Cover malformed JSONL fields/types and malformed schema-card structures; verify errors name the file and JSONL line.
- [x] Cover synthetic terminology code lookup, label lookup, multiple labels, and accent folding. Tests use fabricated entries only.
- [ ] Extend cases for BM25 repeated terms, empty corpora, punctuation, and broader `SchemaCards.closest` ties.
- [ ] Add explicit tests for `ContextBuilder` custom top-k settings, large rendered-context limits, and ensuring example text alone does not create terminology candidates.

### 2. Measure local retrieval quality with safe fixtures

- [x] Add `scripts/retrieval_benchmark.py` with six fabricated cases and fixed expected IDs. It prints aggregate numeric accuracy and p50 latency only; it writes no report.
- [x] Compare the default BM25 settings with two parameter candidates. Neither cleared the 5 percentage-point top-1 improvement threshold, so defaults remain unchanged.
- [ ] Broaden the benchmark before considering stemming, query expansion, or embeddings. Do not add embeddings unless they clear the documented quality/latency thresholds without a heavy runtime dependency.

### 3. Validate file handling and context limits

- [x] Test missing optional files and malformed example/index/card files; production profile snapshots remain required and fail closed.
- [ ] Add explicit oversized-file behavior and measure rendered context size for normal and worst-case inputs.
- [x] Assert deterministic retrieval/context ordering on fixed synthetic fixtures.
- [x] Verify isolated context construction works without `VIRTUOSO_ENDPOINT` and does not import `nl2sparql.virtuoso`.

### 4. Run local tests

From the repository root:

```bash
PYTHONPATH=src python -m pytest -q tests/test_context.py
PYTHONPATH=src python -m pytest -q
```

The first command is the focused context suite. The second is required after changes. Latest results are **36 context tests passed** and **163 full-suite tests passed**. These tests validate code against synthetic fixtures and committed snapshots; they do not prove snapshot freshness or real-graph query behavior.

## Santiago/run-machine work — dev graph required

**Current state:** blocked on run-machine access. No `.env` or generated terminology index is present in this checkout, and no real-graph profile, index build, or query evaluation was performed locally.

Use only the configured run machine and the HTTP endpoint documented in the root README. Never place graph data, patient-level results, generated terminology files, or raw run reports in Git.

### 5. Verify and profile the dev graph

- [ ] Confirm the configured dev endpoint and documented patient/ontology graph IRIs on the run machine.
- [ ] Run `scripts/profile_data.py` against dev, writing to a temporary directory rather than overwriting committed files:

  ```bash
  profile_dir="$(mktemp -d)"
  python scripts/profile_data.py --endpoint http://localhost:8890/sparql --data-dir "$profile_dir"
  diff -u data/property_kinds.json "$profile_dir/property_kinds.json"
  diff -u data/schema_cards.json "$profile_dir/schema_cards.json"
  ```

- [ ] Check that the profile is non-empty and compare classes, properties, kinds, and counts with the committed snapshots. Summarize only sanitized differences.
- [ ] Refresh committed snapshots only when the current graph supports the change; rerun focused and full tests after any refresh.
- [ ] Confirm profile generation remains an offline maintenance action and is never called by the API request path.

### 6. Build and validate terminology candidates locally on the run machine

- [ ] Generate `assets/terminology_index.jsonl` from the authorized ontology/dev endpoint using `scripts/build_terminology_index.py`.
- [ ] Keep the generated asset local and ignored. Do not commit it, print its contents in shared logs, or include terminology entries in this guide.
- [ ] Run synthetic loader/ranking tests locally, then run a controlled evaluation with the generated index enabled. Share aggregate metrics and sanitized error categories only.
- [ ] Confirm representative English requests produce useful candidates across the supported code systems, including cases where labels are multilingual. Do not expose raw terminology labels in public artifacts.
- [ ] Check index build duration, number of entries as an aggregate, memory use, and request-time lookup latency; report only safe aggregate figures.

### 7. Validate context against actual dev behavior

- [ ] Run the configured LLM evaluation with the fresh dev-derived snapshots/index. Compare verdict/answer quality, empty-result rate, retries, latency, and tokens against the root README baseline.
- [ ] For selected examples, verify that retrieved schema terms and code candidates correspond to terms used by successful dev queries. Keep raw requests, SPARQL, and patient-level rows on the run machine.
- [ ] Review false retrievals and missed candidates. Change retrieval rules only when the evaluation provides repeatable evidence; add synthetic regressions for each corrected behavior.
- [ ] Verify updates to examples or snapshots do not change the fixed API fields, literal-translation behavior, privacy decisions, or database-independent request path.

## Santiago/run-machine work — perf graph required

### 8. Measure end-to-end impact at scale

- [ ] Use repeated isolated evaluation runs with the perf graph only for scale-sensitive behavior. Keep data profiling and correctness comparisons on dev.
- [ ] Compare baseline and candidate context configurations for retrieval quality, model tokens, request latency, memory, and query execution time where applicable.
- [ ] Report medians and spread across repeated runs; separate context construction time from model and query execution time when instrumentation permits.
- [ ] Adopt a more expensive retrieval method or larger context only when repeatable quality gains justify its latency, memory, and operational cost.

## Decisions and constraints

- [ ] Agree with the team before changing the example corpus format, retrieval defaults, or shared context interfaces.
- [ ] Keep RDF access in profiling/evaluation scripts only. No Virtuoso call may be reachable from `POST /nl-to-sparql`.
- [ ] Keep restricted terminology and generated terminology-derived assets out of Git and public documentation. Never add patient data, secrets, or run-machine credentials.
- [ ] Coordinate with teammate A before changing `factory.py` snapshot validation.
- [ ] Preserve the API contract and shared interfaces unless the team approves a change.
- [ ] Update this checklist with the tested commit, local test summaries, snapshot comparison outcome, aggregate evaluation metrics, and remaining blockers. Do not report local synthetic tests as real-graph verification.

## Handoff summary

**Local contributors:** complete sections 1–4 using synthetic data; run focused and full test suites; push the branch and report the commit and results.

**Santiago:** complete sections 5–8 on the configured run machine; keep restricted assets and raw reports local; report sanitized snapshot and evaluation summaries. If graph or model access is unavailable, leave those items pending rather than treating offline tests as a substitute.
