# Data and Context — work status and handoff

This guide separates completed local work from real-graph validation. The root [README](../../README.md) is the project-wide source of truth; [docs/DATA.md](../DATA.md) describes RDF graph layout and data conventions.

## Ownership and status

| Work | Owner | Status | What the status means |
| --- | --- | --- | --- |
| Retrieval, loaders, deterministic behavior, synthetic tests, and local benchmark | You / local contributor | **Complete locally** | Implemented and exercised with synthetic fixtures and checked-in snapshots. |
| Sanitized RDF profile comparison helper | You / local contributor | **Implemented; not executed** | `scripts/compare_data_profile.py` is written, but has not been run per the code-only request. |
| Final local checks and branch handoff | You / local contributor | **Complete** | Focused/full tests and benchmark were rerun; the `guardjudge` branch was pushed. See evidence below. |
| Dev graph profile, real terminology index, and actual-data evaluation | Santiago | **Pending run-machine access** | No real graph was available in this checkout; these steps have not been verified. |
| Perf graph measurements | Santiago | **Pending perf access** | No real performance-graph timings have been collected. |
| Work verified both locally and against real graph data | — | **None yet** | Passing synthetic tests does not establish real-graph compatibility or retrieval quality. |

The existing real-data integration path is offline: `scripts/profile_data.py` produces local RDF-derived snapshots, and `scripts/build_terminology_index.py` builds a local terminology index. The application reads those files at request time. **Do not add a live Virtuoso connection to `ContextBuilder`, pipeline construction, or `POST /nl-to-sparql`.**

## Completed by you locally

- [x] Confirmed `ExampleBank`, `SchemaCards`, `TerminologyIndex`, and `ContextBuilder` work from local files.
- [x] Corrected retrieval behavior for empty/stopword queries, nonpositive and oversized `k`, normalized exact-question exclusion, and deterministic tie ordering.
- [x] Added clear path- and line-specific validation errors for malformed examples, schema cards, and terminology index entries; pipeline loader errors include the file path.
- [x] Added tests for malformed/missing optional inputs, synthetic terminology lookup, ranking, ordering, custom top-k settings, large synthetic files, and operation without `VIRTUOSO_ENDPOINT` or a Virtuoso import.
- [x] Kept BM25 defaults: the synthetic benchmark did not meet the threshold for changing them.
- [x] Measured prompt sizes for typical and expanded synthetic context. These are measurements, not an enforced context budget.
- [x] Preserved the factory-owned fail-closed validation for required profile snapshots; coordinate with teammate A before changing it.
- [x] Added `scripts/compare_data_profile.py` for numeric-only snapshot comparison. It reports aggregate additions/removals/changes without printing schema terms, properties, or profile entries.

### Local evidence

**Tested code commit:** `7aca015c48701b70c89eac7c7a7d3d257cbe6d67` (`Refine data and context retrieval`). The focused suite, full suite, and benchmark below were run on this code immediately before the commit. The evidence was recorded in a follow-up guide commit. The pushed branch is `guardjudge`.

```text
PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_context.py
41 passed

PYTHONPATH=src .venv311/bin/python -m pytest -q
168 passed, 1 existing Starlette/anyio deprecation warning

PYTHONPATH=src .venv311/bin/python scripts/retrieval_benchmark.py
14 synthetic cases; 92.9% top-1; 100% top-3 for each of three settings
```

The numeric-only synthetic benchmark used 14 fabricated cases. Default and candidate BM25 settings each scored **92.9% top-1** and **100% top-3**; their p50 retrieval latency was **0.0041 ms**, **0.0042 ms**, and **0.0041 ms**. No candidate improved top-1 by the required 5 percentage points, so defaults remain unchanged. Typical and expanded synthetic rendered prompts were **668** and **37,624 characters**. These are local synthetic measurements, not an LLM or real-graph evaluation.

The profile comparison helper was added after these recorded test and benchmark runs. It has not been executed; the evidence above does not cover that new script.

## Your local work — complete; no RDF graph required

There are no outstanding local implementation tasks in the current checklist. The profile-comparison helper was added after the recorded verification and was intentionally not executed; run it as part of the Santiago profile workflow below. The previously verified code was pushed on `guardjudge`. The commands used for that recorded verification were:

```bash
PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_context.py
PYTHONPATH=src .venv311/bin/python -m pytest -q
PYTHONPATH=src .venv311/bin/python scripts/retrieval_benchmark.py
git diff --check
```

- [x] Reran focused and full tests plus the numeric-only synthetic benchmark after the code changes.
- [x] Pushed the `guardjudge` branch. The tested code commit is identified above; the evidence-only guide update followed it.
- [x] Reviewed the committed file list: no `.env`, generated `assets/` terminology files, graph data, patient-level results, or raw evaluation reports were included.

## Remaining work for Santiago — actual dev RDF graph required

**Status:** not run from this checkout. It has no `.env` or generated `assets/` terminology index, and there is no recorded real-graph profile or evaluation. Use the configured run machine and the endpoint/IRIs documented in the root README and [docs/DATA.md](../DATA.md).

### Dev graph and metadata

- [ ] Confirm the configured dev endpoint and documented patient/ontology graph IRIs.
- [ ] Profile into a temporary directory; compare both generated snapshots with the committed ones before considering a refresh:

  ```bash
  profile_dir="$(mktemp -d)"
  python scripts/profile_data.py --endpoint http://localhost:8890/sparql --data-dir "$profile_dir"
  python scripts/compare_data_profile.py --profile-dir "$profile_dir"
  ```

- [ ] Confirm the profile is non-empty; review the comparison script's aggregate counts for differences in classes, properties, kinds, and schema terms. Do not paste raw profile files or entry-level diffs into shared reports.
- [ ] Refresh committed snapshots only when the graph profile supports the change, then rerun focused and full local tests.
- [ ] Keep profiling an offline maintenance action; never call it from the API request path.

### Terminology index and actual dev evaluation

- [ ] Build `assets/terminology_index.jsonl` with `scripts/build_terminology_index.py` using authorized local access. Keep this generated, restricted-terminology-derived file ignored and local.
- [ ] Measure build duration, aggregate entry count, memory, and request-time candidate lookup latency. Share aggregate numbers only.
- [ ] Run configured LLM evaluation with the dev-derived snapshots/index. Compare answer quality, empty-result rate, retries, latency, and tokens with the root README baseline.
- [ ] Check representative successful dev cases for useful schema/code retrieval. Keep raw requests, SPARQL, terminology labels, and patient-level results on the run machine.
- [ ] Review false retrievals and missed candidates; make retrieval changes only when repeatable evidence supports them, and add synthetic regression tests for each change.
- [ ] Verify real-data configuration preserves API fields, literal translation, privacy decisions, and the database-independent request path.

### Perf graph measurements

- [ ] Use repeated isolated runs on the perf graph for scale-sensitive evaluation only. Keep correctness and profile comparison on dev.
- [ ] Compare context/retrieval candidates for quality, model tokens, latency, memory, and query execution time where relevant; report medians and spread.
- [ ] Adopt larger context or more expensive retrieval only when repeatable gains justify the operational cost. Keep BM25 as default absent that evidence.

## Constraints for all contributors

- Keep RDF access in profiling/evaluation scripts only. No database call may be reachable from `POST /nl-to-sparql`.
- Keep restricted terminology and generated terminology-derived assets out of Git and public documentation. Never add patient data, secrets, or run-machine credentials.
- Do not change API fields or shared interfaces without team agreement. Coordinate with teammate A before changing factory-owned snapshot validation.
- Local synthetic tests and benchmarks are not real-graph verification. Add sanitized snapshot and aggregate run-machine results only after Santiago completes those checks.
