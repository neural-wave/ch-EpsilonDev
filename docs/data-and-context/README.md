# Data and Context — Santiago handoff

This guide records what Adrika completed in Module C and the remaining work for Santiago on the configured run machine. The root [README](../../README.md) is the project-wide source of truth; [docs/DATA.md](../DATA.md) describes the RDF graph layout and data conventions.

## Handoff status

| Area | Status | Owner / next action |
| --- | --- | --- |
| Retrieval, local-file loading, context construction | Implemented and locally verified | Adrika's local work is complete. |
| RDF snapshot generation and comparison tools | Implemented; synthetic comparison tests pass | Santiago must run the tools against the actual dev graph and review aggregate drift. |
| Terminology index builder and runtime loader | Implemented; real index not built in this checkout | Santiago must build it locally from the authorized ontology endpoint and evaluate it. |
| Real dev-graph evaluation | Not verified | Santiago must run LLM evaluation and query execution on dev. |
| Perf-graph measurement | Not verified | Santiago must run repeated scale measurements on perf after dev validation. |
| Verified both locally and against real RDF | No | Local synthetic results are not evidence of real-graph behavior. |

**Access in Adrika's checkout, checked 2026-10-17:** `.env` is absent, and localhost ports 8890 (dev) and 8891 (perf) are closed. No real RDF profile, terminology index, or graph evaluation was performed here. The `guardjudge` branch was pushed at `7a7713698af27ff043c574dc611decddc9bb903c`.

## What Adrika completed

- Refined example, schema-card, and terminology retrieval: empty/stopword queries and nonpositive `k` return no candidates; exact-example exclusion normalizes case, accents, and whitespace; ties are deterministic.
- Added clear validation errors for malformed examples, schema cards, and terminology-index rows. Pipeline loader errors identify the failing file path.
- Preserved fail-closed validation for required profile snapshots in `factory.py`; coordinate with teammate A before changing that factory-owned code.
- Confirmed the request path reads local snapshots/assets only. It does not query Virtuoso. Do not add graph access to `ContextBuilder`, pipeline construction, or `POST /nl-to-sparql`.
- Added `scripts/compare_data_profile.py`. It compares committed snapshots with a candidate profile and prints aggregate additions/removals/changes only, without printing class/property names or profile entries. It rejects missing, malformed, or empty profiles.
- Added synthetic comparator cases for equal profiles, aggregate differences, and empty-profile rejection.
- Kept BM25 defaults because the synthetic benchmark did not justify a change.

### Local evidence

The profile-comparison helper is in commit `51b817f77c66bf27c9f54fe12324f1e1a25154d4`; its synthetic tests are in `471054aef4847b94e210bc31ed07a487c136cfbe`.

```text
PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_context.py
41 passed

PYTHONPATH=src .venv311/bin/python -m pytest -q tests/test_profile_comparison.py
3 passed

PYTHONPATH=src .venv311/bin/python -m pytest -q
171 passed, 1 existing Starlette/anyio deprecation warning
```

The synthetic retrieval benchmark used 14 fabricated cases. Default and both candidate BM25 settings scored **92.9% top-1** and **100% top-3**; p50 retrieval latency was **0.0041 ms**, **0.0042 ms**, and **0.0041 ms**. No candidate met the 5-point top-1 improvement threshold. Typical and expanded synthetic rendered prompts were 668 and 37,624 characters. These are synthetic measurements, not real-graph or LLM results. The benchmark was run before the profile-comparison helper was added; that helper does not alter retrieval.

## Existing code Santiago can use

| Task | Existing tool / code | Real-data result still needed |
| --- | --- | --- |
| Profile dev RDF | `scripts/profile_data.py` | Generate candidate `property_kinds.json` and `schema_cards.json` in a temporary directory. |
| Compare snapshots | `scripts/compare_data_profile.py` | Run it on the generated profile and record its sanitized aggregate output. |
| Build terminology index | `scripts/build_terminology_index.py` | Generate the ignored local `assets/terminology_index.jsonl` from the authorized endpoint. |
| Load context | `src/nl2sparql/context/` and `factory.py` | Evaluate with the generated local assets; no request-time database calls. |
| Evaluate system | `eval/run_eval.py`, `scripts/runbox.sh` | Run the configured LLM and optional dev execution; review raw reports only on the run machine. |

There is no known unimplemented Data and Context tool that blocks these run-machine checks. If real results reveal a retrieval or compatibility issue, make a targeted code change then add synthetic regression coverage.

## Santiago's required run-machine work

Run this on Santiago's configured run machine, where Virtuoso, the LLM tunnel, and `.env` are configured. Use the branch above. Keep `assets/`, `.env`, raw requests, SPARQL, terminology labels, patient-level results, and evaluation reports on that machine; share sanitized summaries only.

### 1. Check out the branch and confirm services

From the repository root:

```bash
git fetch origin guardjudge
scripts/runbox.sh status
scripts/runbox.sh test --branch guardjudge
```

The test command runs the full unit suite in the branch worktree. This is a local synthetic/code check, not real-graph validation.

### 2. Profile the dev graph and compare snapshots

Generate into a temporary directory. Do not let the profile script overwrite committed snapshots before the comparison is reviewed.

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

Comparator exit codes:

- `0`: candidate and committed snapshots match.
- `1`: profile drift exists; review it locally before deciding whether snapshots should change.
- `2`: a profile file is missing, malformed, or empty.

Review the numeric additions/removals/changes for properties, classes, class-property pairs, and schema terms. Do not paste raw profiles or entry-level diffs into shared reports. Refresh committed snapshots only when the real graph supports the change. After a refresh, rerun focused and full tests in the branch worktree. Profiling remains a maintenance action, never part of the API request path.

### 3. Build the terminology index in the evaluation worktree

The ignored asset must be built in the same worktree used for evaluation; ignored `assets/` files are not shared between worktrees.

```bash
(cd "$worktree" && \
  PYTHONPATH="$worktree/src" "$repo_root/.venv/bin/python" \
  scripts/build_terminology_index.py \
  --endpoint http://localhost:8890/sparql)

git -C "$worktree" check-ignore assets/terminology_index.jsonl
```

The generated file is derived from restricted terminologies. Keep it ignored and local. Record build duration, aggregate entry count, memory use, and lookup latency without sharing file contents or labels. If the script or data fails, keep the error details local and report a sanitized error category.

### 4. Evaluate context and query behavior on dev

Run the LLM evaluation and compliant-query execution for the branch worktree:

```bash
scripts/runbox.sh eval --execute --branch guardjudge
```

Review the generated report locally. Compare verdict accuracy, false positives/negatives, forbidden requests marked compliant, empty-result rate, execution errors, retries, latency, and token counts against the baseline in the root README. Check whether representative successful queries retrieved useful schema and code candidates. Do not copy raw questions, SPARQL, terminology labels, patient results, or reports into Git or this guide.

If evaluation exposes a repeatable context or retrieval defect, implement a narrow fix and add synthetic trigger/near-miss tests. Confirm that literal translation, privacy decisions, API fields, and database independence are preserved. Do not refresh snapshots or change defaults based on a single unexplained result.

### 5. Measure on perf after dev checks

Use the 100,000-patient perf graph only after dev correctness is reviewed. Run isolated repeated measurements for scale-sensitive candidates and report medians/spread for quality, model tokens, latency, memory, and query execution time where available. Keep raw evaluation output and graph results local. Retain BM25 and current context sizes unless repeatable measurements show a useful gain; only implement retrieval changes supported by the measurements.

## What to report back

Share only a sanitized summary:

- whether the dev service and expected graph IRIs were confirmed;
- whether the profile snapshots matched, or aggregate counts of additions/removals/changes;
- terminology-index build duration, aggregate entry count, and resource/lookup measurements;
- evaluation accuracy, false positive/negative counts, forbidden-compliance count, empty-result/error counts, retries, latency, and tokens versus the root README baseline;
- perf medians/spread and any evidence-backed follow-up change;
- remaining blockers.

Do not share patient-level results, raw questions or SPARQL, terminology entries/labels, `.env`, credentials, generated `assets/`, or raw reports.

## Constraints

- RDF access belongs in profiling and evaluation scripts only. No database call may be reachable from `POST /nl-to-sparql`.
- Do not change API fields, shared interfaces, or factory-owned snapshot validation without team agreement.
- Do not commit patient data, restricted terminology or derived assets, `.env`, secrets, or run-machine details.
- Keep the module guide's local synthetic evidence and real-graph evidence separate.
