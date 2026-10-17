# Data and Context — work status and handoff

This guide separates completed local work from real-graph validation. The root [README](../../README.md) is the project-wide source of truth; [docs/DATA.md](../DATA.md) describes RDF graph layout and data conventions.

## Ownership and status

| Work | Owner | Status | What the status means |
| --- | --- | --- | --- |
| Retrieval, loaders, deterministic behavior, synthetic tests, and local benchmark | You / local contributor | **Complete locally** | Implemented and exercised with synthetic fixtures and checked-in snapshots. |
| Sanitized RDF profile comparison helper | You / local contributor | **Synthetic tests pass; real profile pending** | The helper is covered with fabricated profile fixtures; it has not been run against an RDF-derived profile. |
| Final local checks and branch handoff | You / local contributor | **Complete** | The helper’s synthetic tests and full suite pass; the `guardjudge` branch has been pushed. |
| Dev graph profile, real terminology index, and actual-data evaluation | Santiago | **Pending run-machine access** | No real graph was available in this checkout; these steps have not been verified. |
| Perf graph measurements | Santiago | **Pending perf access** | No real performance-graph timings have been collected. |
| Work verified both locally and against real graph data | — | **None yet** | Passing synthetic tests does not establish real-graph compatibility or retrieval quality. |

The existing real-data integration path is offline: `scripts/profile_data.py` produces local RDF-derived snapshots, and `scripts/build_terminology_index.py` builds a local terminology index. The application reads those files at request time. **Do not add a live Virtuoso connection to `ContextBuilder`, pipeline construction, or `POST /nl-to-sparql`.**

## Code implementation status

There are **no known unimplemented Data and Context code items** in the current checklist. The profile generator, sanitized profile comparator, terminology-index builder, local-file loaders, and request-time context integration are already implemented. The unchecked Santiago items below are required runs and evidence collection, not requests to write those tools. Any further code change depends on issues found when Santiago runs them.

| Capability | Code status | Still required |
| --- | --- | --- |
| Generate profile snapshots from dev RDF | Implemented in `scripts/profile_data.py` | Run against Santiago's dev graph; compare output with committed snapshots. |
| Compare profile snapshots without exposing entries | Implemented and covered by synthetic tests in `scripts/compare_data_profile.py` | Run against the generated real profile; record aggregate result. |
| Generate restricted terminology index | Implemented in `scripts/build_terminology_index.py` | Build the local ignored asset on Santiago's machine and measure it there. |
| Load context assets at request time | Implemented in `ContextBuilder` and the factory | Validate with the real generated assets and configured evaluation. |
| Tune retrieval/performance | No evidence-backed change identified | Measure on dev/perf first; implement a change only if results justify it. |

## Completed by you locally

- [x] Confirmed `ExampleBank`, `SchemaCards`, `TerminologyIndex`, and `ContextBuilder` work from local files.
- [x] Corrected retrieval behavior for empty/stopword queries, nonpositive and oversized `k`, normalized exact-question exclusion, and deterministic tie ordering.
- [x] Added clear path- and line-specific validation errors for malformed examples, schema cards, and terminology index entries; pipeline loader errors include the file path.
- [x] Added tests for malformed/missing optional inputs, synthetic terminology lookup, ranking, ordering, custom top-k settings, large synthetic files, and operation without `VIRTUOSO_ENDPOINT` or a Virtuoso import.
- [x] Kept BM25 defaults: the synthetic benchmark did not meet the threshold for changing them.
- [x] Measured prompt sizes for typical and expanded synthetic context. These are measurements, not an enforced context budget.
- [x] Preserved the factory-owned fail-closed validation for required profile snapshots; coordinate with teammate A before changing it.
- [x] Added `scripts/compare_data_profile.py` for numeric-only snapshot comparison. It reports aggregate additions/removals/changes without printing schema terms, properties, or profile entries.
- [x] Added synthetic tests for identical profiles, aggregate-only differences, and empty-profile rejection.

### Local evidence

**Profile comparison code:** `51b817f77c66bf27c9f54fe12324f1e1a25154d4`. **Synthetic regression tests:** `471054aef4847b94e210bc31ed07a487c136cfbe`. The test and full-suite results below were run on these code changes before their commits. Real RDF profile comparison has not been run.

```text
PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_context.py
41 passed

PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_profile_comparison.py
3 passed

PYTHONPATH=src .venv311/bin/python -m pytest -q
171 passed, 1 existing Starlette/anyio deprecation warning

PYTHONPATH=src .venv311/bin/python scripts/retrieval_benchmark.py
14 synthetic cases; 92.9% top-1; 100% top-3 for each of three settings
```

The numeric-only synthetic benchmark used 14 fabricated cases. Default and candidate BM25 settings each scored **92.9% top-1** and **100% top-3**; their p50 retrieval latency was **0.0041 ms**, **0.0042 ms**, and **0.0041 ms**. No candidate improved top-1 by the required 5 percentage points, so defaults remain unchanged. Typical and expanded synthetic rendered prompts were **668** and **37,624 characters**. These are local synthetic measurements, not an LLM or real-graph evaluation.

The retrieval benchmark was last run before the profile comparison helper was added; the helper does not change retrieval behavior. Its comparison logic was exercised by the three synthetic tests above. **Santiago must still run the CLI against a profile generated from the actual dev graph** as described below.

## Your local work — complete; no RDF graph required

There are no outstanding local implementation tasks in the current checklist. The profile-comparison helper now has synthetic test coverage; running it against Santiago's generated profile remains part of the graph workflow below. The commands used for local verification are:

```bash
PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_context.py
PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_profile_comparison.py
PYTHONPATH=src .venv311/bin/python -m pytest -q
PYTHONPATH=src .venv311/bin/python scripts/retrieval_benchmark.py
git diff --check
```

- [x] Ran the profile-comparison focused tests and full suite; results are recorded above.
- [x] Pushed the helper and synthetic tests on `guardjudge`; this guide records the handoff requirements.
- [x] Confirmed the helper and fixtures contain only aggregate-reporting logic and synthetic profile data; no `.env`, generated `assets/` terminology files, graph data, patient-level results, or raw evaluation reports were added.

## Remaining execution and validation for Santiago — actual dev RDF graph required

**Status:** not run from this checkout. It has no `.env` or generated `assets/` terminology index, and there is no recorded real-graph profile or evaluation. Use the configured run machine and the endpoint/IRIs documented in the root README and [docs/DATA.md](../DATA.md).

The tools and commands in this section already exist. The open boxes track running them against the real environment, reviewing the results, and making evidence-driven follow-up changes if needed.

**Required real-data test for the new helper:** the three passing unit tests use fabricated profile files only. They do not validate the CLI with Santiago's RDF-derived snapshots. Santiago must run the workflow below on the configured dev graph and record the sanitized aggregate result before marking profile freshness complete.

### Dev graph and metadata

- [ ] Confirm the configured dev endpoint and documented patient/ontology graph IRIs.
- [ ] Profile into a temporary directory; compare both generated snapshots with the committed ones before considering a refresh:

  ```bash
  profile_dir="$(mktemp -d)"
  python scripts/profile_data.py --endpoint http://localhost:8890/sparql --data-dir "$profile_dir"
  python scripts/compare_data_profile.py --profile-dir "$profile_dir"
  ```

- [ ] Confirm the CLI outcome: exit code **0** means the snapshots match; **1** means profile drift was found; **2** means a file is missing, malformed, or empty. Investigate exit code 1 on the run machine before refreshing snapshots.
- [ ] Review aggregate additions/removals/changes for properties, classes, class-property pairs, and schema terms. Do not paste raw profile files or entry-level diffs into shared reports.
- [ ] Run `PYTHONPATH=src python -m pytest -q tests/test_profile_comparison.py` on the pushed branch to confirm its synthetic tests still pass in the run-machine environment. This complements the real-profile CLI run; neither substitutes for the other.
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
