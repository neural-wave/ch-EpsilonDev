# Project Status Checklist

Status checked on **17 October 2026** against the root [README](README.md), module guides, current repository files, and the pushed `guardjudge` branch (`bdf5f0e4ba20e73f546e038e4f1a50211fa5fed1`). The README remains the project-wide source of truth. This checklist distinguishes implemented code from work that still needs external decisions, run-machine access, or submission action.

## Done: base implementation and local verification

- [x] End-to-end pipeline components are built and wired together.
- [x] FastAPI endpoint and fixed response contract are implemented.
- [x] Input validation and normalization are implemented.
- [x] Module A has a configurable whole-request timeout and an HTTP concurrency test command.
- [x] Intent and query agents, prompts, and OpenAI-compatible LLM client are present.
- [x] Context retrieval, schema cards, example bank, and terminology-index lookup code are present.
- [x] SPARQL parser, Judge diagnostics, compliance rules, and shared retry flow are implemented.
- [x] Demo, evaluation runner, Virtuoso helper, profile/index builders, and database setup scripts are present.
- [x] Local full test suite passes after merging modules A, B, C and D: **243 passed** (17 October). Local evidence only: it does not verify the actual RDF graphs.
- [x] Baseline evaluation is recorded in the root README: stub 87% verdict accuracy; Qwen3.5-9B 90%, zero forbidden requests marked compliant, 2.8 s median latency.
- [x] Jury confirmed the system must be database-agnostic at request time; compliant-query execution is optional; the hackathon API returns the query for forbidden requests.

## Data and Context (Module C)

### Code complete locally

- [x] Refined example, schema-card, and terminology candidate retrieval, including empty queries, `k` limits, normalized exact-question exclusion, and deterministic ties.
- [x] Added path/line-specific validation for malformed context files and loader errors naming the source path.
- [x] Preserved fail-closed validation for required schema/profile snapshots.
- [x] Added `scripts/compare_data_profile.py` for aggregate-only snapshot comparison; synthetic comparison tests pass.
- [x] Confirmed request-time context loading does not require a Virtuoso connection.
- [x] Kept BM25 defaults because the synthetic benchmark did not meet the change threshold.

### Remaining: real-data work for Santiago

- [ ] Profile the dev RDF graph into a temporary directory and compare both snapshots with `scripts/compare_data_profile.py`. No profile was produced in this checkout: `.env` is absent and dev port 8890 is closed.
- [ ] Refresh committed snapshots only if the measured dev profile supports the change; rerun tests after a refresh.
- [ ] Build `assets/terminology_index.jsonl` on the configured run machine. The builder and loader exist; the generated restricted-terminology-derived index is not in Git.
- [ ] Evaluate context with the real generated index and configured LLM. Report aggregate retrieval quality, empty-result rate, latency, tokens, memory, and build duration.
- [ ] Use real evaluation evidence before changing BM25 defaults, adding embeddings, changing context size, or adding English label support.
- [ ] Measure scale-sensitive behavior on the perf graph after dev correctness is reviewed. Perf port 8891 is closed in this checkout.
- [ ] Add SIB-format examples only when validated and approved for inclusion.

Local evidence: `tests/test_context.py` passed **41 tests**; `tests/test_profile_comparison.py` passed **3 tests**; the full suite passed **171 tests**. The synthetic benchmark covered 14 fabricated cases: default and both candidates scored 92.9% top-1 and 100% top-3, so defaults stayed unchanged. See [docs/data-and-context/README.md](docs/data-and-context/README.md) for full evidence and the Santiago runbook.

## Guard and Judge (Module D)

### Code complete for current offline scope

- [x] Fixed R1 `PropertyKinds` wiring and restored inverse identifier-path detection.
- [x] Synthetic Guard coverage exercises R1–R5 and the current nested-query, graph, `VALUES`, filter, `BIND`, aggregate, grouping, property-path, identity-transform, `OPTIONAL`, and `UNION` cases with valid near-misses.
- [x] Judge/parser diagnostics cover prefixes, syntax, unknown SPHN terms and suggestions, inference per code pattern, patient graph counts, and Virtuoso extension parsing, with valid neighboring cases.
- [x] Preserved forbidden-request no-retry behavior and the rule that query violations never become compliant.
- [x] Kept request-time Guard/Judge behavior database-agnostic.

### Remaining: run-machine validation and policy decisions

- [ ] Santiago: run fresh LLM evaluation and dev query execution; compare verdict accuracy, false positives/negatives, forbidden leaks, retries, latency, and tokens with the root README baseline.
- [ ] Santiago: reconcile fresh corpus outcomes for #01, #07, and #08 with post-retry logs. Keep raw queries, question text, and reports on the run machine.
- [ ] Santiago: reproduce #05 and #10 Virtuoso failures and investigate `NULL` and nested-`GRAPH` error patterns. Add static diagnostics only for reproducible patterns, each with a synthetic valid near-miss.
- [ ] Team/organizers: decide fine-grained grouping, `MIN`/`MAX`, minimum group size, `SERVICE`, and singleton-group policy. Keep current behavior unchanged until decisions arrive.
- [ ] Team: agree on selective-pattern ordering guidance before any Judge diagnostic is added.
- [ ] Santiago: measure repeated isolated perf-graph runs before proposing ordering advice or rewrites. Prove rewrite equivalence with synthetic regressions and dev result comparisons first.
- [ ] Make further Guard/Judge code changes only when an actionable policy decision, reproducible Virtuoso error, or specific test/evaluation defect supports them.

Local evidence recorded in [docs/guard-and-judge/README.md](docs/guard-and-judge/README.md): Guard **68 passed**, focused Guard/Judge/SPARQL/pipeline suite **116 passed**, repository full suite **171 passed**. The documented local stub result is not a fresh LLM evaluation and does not verify real-graph behavior.

## Integration

- [ ] Decide where the API will run during judging and how GPU access will be provided.
- [ ] Run `python -m nl2sparql.load_test --url http://localhost:8080 --questions eval/questions.jsonl --requests 32 --concurrency 8 --require-llm` on the run machine; record errors, latency, throughput, and tokens. The command is ready, but the real LLM load run is pending.
- [ ] Verify clean-environment installation and startup of the final A revision. A clean checkout of the earlier revision passed on 17 October; repeat the check after the new A changes are included.
- [ ] Run the existing LLM evaluation on the final A branch and compare it with the README baseline before merging; leave evaluation code to module E.
- [ ] Finish the README for the jury after the deployment answer and teammates' full names and roles are confirmed.

## Agents and prompts

- [ ] Choose and record the final model configuration.
- [ ] Improve prompts so complex requests, especially #01 and #08, produce valid SPARQL and fewer allowed queries return zero rows.
- [ ] Reduce the approximate 3,000 tokens per request, subject to measured quality.
- [ ] Measure intent-agent false alarms and prompt-injection resistance.

## Evaluation and demo

- [ ] Grow the labeled evaluation set to 80+ questions, including paraphrases, prompt-injection attempts, and organizer-approved gray zones.
- [ ] Run evaluation questions in parallel if quality and report ordering remain stable.
- [ ] Store reference results for correctness checks without including patient data or restricted terminology.
- [ ] Build a ten-run timing harness on the 100,000-patient performance database.
- [ ] Polish the demo and prepare final numbers for the report and video.

## Organizer questions

- [ ] Confirm judging deployment hardware and how the team can keep GPU access.
- [ ] Confirm whether result correctness is evaluated, or only verdicts and timings.
- [ ] Resolve whether only `SELECT` is accepted.
- [ ] Confirm query timing method, cache conditions, timeout, and dataset size.
- [ ] Resolve gray zones: fine-grained grouping, `MIN`/`MAX` that may identify a patient, and minimum group size.
- [ ] Confirm whether `SERVICE` and singleton groups are permitted.
- [ ] Confirm whether adversarial requests will be included and whether requests are English-only.
- [ ] Confirm conventions for interpreting requests such as “diagnosed with X”.
- [ ] Ask whether additional SPHN example queries are available.

## Submission checklist (deadline: Sunday 18 October 2026, 11:00)

- [ ] Push the code to the official `NW_26_<Company>_<Team>` repository and ensure collaborators are invited.
- [ ] Provide the jury README: overview, run instructions, dependencies, and every member's full name and role.
- [ ] Confirm `pip install -r requirements.txt` works in a clean environment and `python -m nl2sparql` starts without debugging.
- [ ] Write the report (maximum 3 pages): approach, architecture and rationale, results and figures, challenges and failed ideas, and disclosure of AI-agent/LLM coding-assistant use.
- [ ] Produce and watch the video (maximum 2 minutes) showing the goal, relevance, key results, and demo; share it by SwissTransfer.
- [ ] Credit external sources and check that no restricted terminology, derived assets, patient data, secrets, or GPU host details are included.
- [ ] Prepare a 5-minute presentation in case the team wins.

## Notes

- The base pipeline plan marks all 13 implementation steps complete. Unchecked tasks above are quality, real-data validation, policy, or submission work—not missing base architecture.
- The latest local checkout check found `.env` absent and ports 8890/8891 closed. Santiago's actual graph and configured LLM work remains pending.
- Keep `assets/` terminology-derived files, patient data, `.env`, tokens, GPU host details, and raw run-machine reports out of Git and shared documentation.
- Run `python -m pytest -q` after code changes before marking them complete. Real-graph checks remain a separate validation layer.
