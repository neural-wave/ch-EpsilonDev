# Historical project status and remaining-work checklist

> Historical checklist retained for traceability. Its revision, test counts, and backlog are not current verification. Use the [project README](../../README.md) and [validation record](../RESULTS_VALIDATION.md) for current delivery status.

Status reviewed on **17 October 2026** against the root [README](../../README.md), module guides, tracked source/tests/scripts, and current Git state. The root README is the project-wide source of truth. This checklist distinguishes merged implementation, reported evidence, and work that still needs a fresh run, real graph access, team decisions, or submission action.

**Current checkout:** `main` at `49ebc29e2977fa9470bda51854bd7f93bd6a275c`. This is the revision inspected for this checklist; it was **not** test-run in this environment. The root README reports 244 tests passing locally and in pull-request CI, but that report does not establish that exact SHA was tested. The local `.venv311` has no `bin/python` and no usable pytest installation. Rerun the suite on this revision before claiming it verified.

## Completed implementation

- [x] Modules A–E have implementation code in the repository and modules A–D are merged on `main`.
- [x] FastAPI request flow, input validation, configurable timeouts, shared one-retry orchestration, response contract, and no-retry handling for forbidden intent are implemented.
- [x] Intent/query agents, prompt versions, OpenAI-compatible client, context retrieval, schema/profile loading, terminology lookup, Guard, Judge, and shared SPARQL parsing are implemented.
- [x] Parser access is serialized with a lock; an eight-thread parser regression test is present.
- [x] Evaluation runners, runbox scripts, profile/index builders, query checker, profile comparator, retrieval benchmark, load-test command, demo, and Virtuoso helper are present.
- [x] Request-time code is database-agnostic. Virtuoso access is limited to evaluation, maintenance scripts, or optional demo execution.
- [x] API fields, shared model/agent interfaces, literal translation, open-source model requirement, and the rule that query violations cannot become compliant are documented as invariants.
- [x] The root README records the current project evaluation and known issues. See its sections 3, 11, and 13 for the latest shared status.

## Verification required on this revision

- [ ] On a working Python 3.11 environment, run `python -m pytest -q` at the current commit and record the actual result here and in the module guides.
- [ ] Run the focused suites after confirming the full-test environment: `tests/test_context.py`, `tests/test_guard.py`, `tests/test_sparql.py`, `tests/test_agents_and_judge.py`, `tests/test_pipeline_and_api.py`, and `tests/test_profile_comparison.py`.
- [ ] Run `scripts/retrieval_benchmark.py` separately; retain only its aggregate numeric output. Do not change BM25 defaults unless the documented quality/latency thresholds are met on repeatable evidence.
- [ ] Check `git status` and review all files before the official push. The current checkout contains untracked duplicate-named files; do not add these by accident, and confirm their ownership/content before cleaning or sharing them.
- [ ] Update `docs/data-and-context/README.md` and `docs/guard-and-judge/README.md`: both still identify the old `guardjudge` revision and report 171 tests. Replace those with evidence for the merged revision only after it has actually been tested; label graph/LLM results separately from local tests.

## Module A — Integration

- [x] API, pipeline construction, configuration, request validation, timeout handling, and the load-test command are implemented.
- [ ] Run `python -m nl2sparql.load_test --url http://localhost:8080 --questions eval/questions.jsonl --requests 32 --concurrency 8 --require-llm` against the configured LLM endpoint. Record aggregate errors, latency, throughput, and tokens.
- [ ] Confirm judging deployment location and model/GPU access with the team/organizers.
- [ ] Install from a clean environment and verify startup and API smoke checks for the final pushed revision.

## Module B — Agents and prompts

- [x] Intent/query agent implementations, open-source model configuration, prompt versioning, token/timing reporting, console, and evaluation support are present.
- [ ] Raise the query output cap from 900 toward 1,500 tokens if the measured truncation cases improve without hurting quality.
- [ ] Add the proposed query prompt v4 change to group by concept class rather than code node; evaluate it against dev and preserve a blind holdout.
- [ ] Investigate the allowed-request false alarm involving a count that mentions an identifier.
- [ ] Review first-try Judge errors and retry logs for #01/#08; reduce retries only where a validated prompt/context fix preserves verdict quality.
- [ ] Consider intent prompt v5, an Apertus comparison, and token reductions only with measured dev/holdout results. Keep the holdout blind until final selection.
- [ ] Check prompt-injection resistance and intent false alarms on the labeled evaluation set.

## Module C — Data and Context

### Implemented locally / available on the run machine

- [x] Example, schema-card, and terminology retrieval support empty queries, `k` boundaries, normalized exact-question exclusion, and deterministic ranking.
- [x] Context loaders validate malformed inputs with path/line-specific errors; production snapshot validation is fail-closed.
- [x] Aggregate-only snapshot comparison and synthetic regression tests are implemented.
- [x] Synthetic retrieval benchmark did not meet the documented improvement threshold; BM25 remains the default.
- [x] Root README reports the terminology index and coded value sets were built on the run machine. Generated terminology-derived assets remain ignored and must not be committed.

### Still required

- [ ] Santiago: profile the actual dev graph into a temporary directory and compare both snapshots; refresh committed metadata only if the measured profile supports it.
- [ ] Santiago: review profile drift using sanitized aggregate counts only; keep profile files and entry-level diffs local.
- [ ] Santiago: add the 21 dev reference examples to the example bank only after review and ensure official examples never retrieve themselves during evaluation.
- [ ] Santiago/team: assess authorized English ICD-10-GM/CHOP labels for the two missed code cases; keep terminology-derived files and labels out of Git unless explicitly approved.
- [ ] Santiago: run retrieval and configured LLM evaluation with the generated local index; report aggregate quality, empty-result rate, build time, memory, latency, and tokens.
- [ ] Santiago/team: configure a server-side dev Virtuoso query timeout so evaluation cannot saturate the service.
- [ ] Use dev correctness evidence before considering BM25/context changes; use repeated perf measurements only after correctness review.

## Module D — Guard and Judge

### Implemented synthetic behavior

- [x] Guard R1–R5 and Judge/parser diagnostics are implemented, including supplied `PropertyKinds` use, inverse identifier paths, nested query cases, unknown-term suggestions, inference checks, and graph-count diagnostics.
- [x] Tests cover synthetic trigger/near-miss cases for currently agreed policy, and forbidden-request no-retry behavior.
- [x] Database access is absent from the request-time Guard/Judge path.

### Remaining

- [ ] Santiago: run current tests and fresh configured LLM evaluation; compare verdict accuracy, false positives/negatives, forbidden leaks, retries, latency, and tokens with the root README baseline.
- [ ] Santiago: reconcile fresh corpus outcomes for #01, #07, and #08 using post-retry logs; keep raw queries, question text, and reports on the run machine.
- [ ] Santiago: reproduce #05 and #10 Virtuoso failures and investigate reported `NULL`/nested-`GRAPH` issues. Add a static diagnostic only for a reproduced pattern and pair it with a synthetic valid near-miss.
- [ ] Team/organizers: decide fine-grained grouping, `MIN`/`MAX`, minimum group size, `SERVICE`, and singleton-group policy. Preserve current behavior until decisions are recorded.
- [ ] Team: agree on selective-pattern ordering guidance before adding a Judge rule; use repeated perf timings and prove rewrite equivalence on dev before any automatic rewrite.
- [ ] Review SIB validator findings offline; do not add database access or a heavy dependency to the API path without team agreement and evidence.

## Module E — Evaluation, performance, and demo

- [x] Intent, pipeline, query, and local evaluation tooling is present; the root README records the latest run-machine numbers.
- [ ] Grow/maintain a labeled set of at least 80 questions with paraphrases, injection cases, and organizer-approved gray zones, while keeping the holdout blind.
- [ ] Run fresh 31-question and 198-question configured LLM evaluations on the run machine; compare against the root README baseline (90.3% shared accuracy; 91.3% dev and 83.7% holdout on the labeled set, with the listed false-positive/negative and latency/token caveats).
- [ ] Re-run dev execution and classify results as successful, empty, or execution error without sharing patient-level output.
- [ ] Build a ten-run isolated timing harness on the 100,000-patient perf graph; report medians and spread, not raw patient results.
- [ ] Polish and rehearse the demo, report, and presentation after final measurements are available.

## Organizer/team decisions

- [ ] Confirm judging deployment hardware and how model access will be maintained.
- [ ] Confirm whether result correctness is scored in addition to verdicts and timing; confirm accepted query forms and timing/cache/timeout methodology.
- [ ] Resolve gray zones: fine-grained grouping, identifying `MIN`/`MAX`, and minimum group size.
- [ ] Confirm whether federated `SERVICE` and singleton result groups are permitted.
- [ ] Confirm whether adversarial requests/languages are in scope and conventions for requests such as “diagnosed with X”.
- [ ] Ask whether additional approved SPHN example queries are available.

## Submission and release

- [ ] Push the tested revision to the official `NW_26_<Company>_<Team>` repository and invite collaborators.
- [ ] Complete the jury README with run instructions, dependencies, and every member’s full name and role.
- [ ] Confirm clean installation with `pip install -r requirements.txt` and startup with `python -m nl2sparql`.
- [ ] Write the three-page maximum report with approach, architecture, results, challenges, failed ideas, and AI-assistant disclosure.
- [ ] Produce and watch the two-minute maximum demo video; share using the agreed transfer method.
- [ ] Credit external sources and audit the public tree for secrets, patient data, restricted terminology, derived assets, and GPU host details.
- [ ] Prepare the five-minute presentation.

## Evidence and privacy rules

- The root README’s Qwen3.5-9B results (31-question and 198-question evaluations, query execution rates, latency, and tokens) are **reported run-machine evidence**, not a fresh test of this checkout.
- The module handoffs’ 171-test figures and old `guardjudge` SHAs are historical branch evidence; they are not current `main` results.
- Local unit tests, synthetic benchmarks, dev-graph checks, perf timings, and configured LLM evaluations are separate evidence layers. Never describe one as a substitute for another.
- Keep `.env`, tokens, GPU host details, RDF/patient data, raw evaluation logs/results, terminology files, and generated restricted-derived assets out of Git and shared reports.
- After any code changes, rerun focused tests and `python -m pytest -q`; update this checklist with the actual tested commit SHA and counts.
