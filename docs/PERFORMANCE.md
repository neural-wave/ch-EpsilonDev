# Performance log

One place for every measured run: the configuration behind it, the numbers, and what moved. Update it after every change you measure, and paste the same numbers in your pull request.

The jury scores three things: **time per request**, **verdict correctness** against their labels, and **execution time of our query on their machine** (average of 10 isolated runs). Tokens and model size are minor criteria.

## 1. How we measure

| Eval | Command | Questions | Measures | Needs |
|---|---|---|---|---|
| Whole pipeline, shared set | `python eval/run_eval.py --execute` | 31 (16 official + 15 team) | Verdicts, latency, tokens per step, retries | GPU, dev Virtuoso |
| Whole pipeline, labeled set | `python eval/run_eval.py --intent-cases --concurrency 4` | 198 (31 + 167 intent cases) | **The verdict metric**, split dev / holdout | GPU |
| Intent agent alone | `python eval/run_intent_eval.py --concurrency 4` | 198 | Detection, false alarms, right flag, tokens, latency | GPU |
| Query agent vs reference SPARQL | `python eval/run_query_eval.py --concurrency 4` | 87 (11 official + 76 verified; 47 until 17 Oct 14:00) | Exact result, right numbers (`exact_numbers`: the reference's numbers with other labels, rounding or layout), runs on Virtuoso, first-try validity, blocked allowed, query tokens | GPU, dev Virtuoso |
| Query time on 100,000 patients | `python eval/time_on_perf.py eval/reports/query-<date>.json --runs 2` | The queries of a query report | Seconds vs the reference query, timeouts past the client deadline, container restarts | perf Virtuoso |
| Load | `python -m nl2sparql.load_test --url http://localhost:8080 --questions eval/questions.jsonl --requests 32 --concurrency 8 --require-llm` | 15 | Failures, p50/p95, throughput | GPU, running API |

**Rules.**
- **Correctness on dev, timing on perf.** The dev database (2,000 patients) answers reference queries in seconds; on perf (100,000) the official references take minutes or hours, so only timing runs there, as plain runs with a client-side deadline: the anytime `timeout` parameter changes the plan of OPTIONAL + inference queries (FAILURE_ANALYSIS §3.6), so the harness restarts the container after an overrun instead.
- **Dev and holdout.** Questions marked `holdout` are a blind exam: never study them one by one or tune anything on them. A change is kept only if the holdout does not get worse — beyond the run-to-run noise, which is about 4 holdout points (see the noise check in the scoreboard).
- **One change per run.** Prompt version, context size, model, answer cap: change one, measure, commit with the numbers.
- **Strict and as asked.** official-03's reference keeps the principal diagnosis although its question says "any diagnosis rank", so the literal answer scores `same_rows`; `run_query_eval.py` reports the strict counts (the headline, as before) and the as-asked ones (`right_numbers_as_asked`, `as_asked` per split) against that reference without the rank restriction (`AS_ASKED`): rescored on dev, Qwen3.6-27B's full run (21:07) has 48 right numbers of 87 strictly and 49 as asked (official 1 → 2 of 11).
- **Reports** land in `eval/reports/` (git-ignored) and show up in the agents console, <http://localhost:8080/agents>, under **Measurements**.
- Latency is comparable only between runs at the same concurrency. Concurrency 4 adds queueing on an 8-sequence vLLM.
- With `CONTEXT_DEV_REFERENCES=true` only the holdout numbers are meaningful: dev questions retrieve their neighbours' reference queries (same author, often the same topic family), so dev rates go up without saying anything about new questions; compare holdout with holdout and note the flag with every run. Offline, leave-one-out on the 45 dev questions (top 3, no model): an example that uses the reference's main class 15 → 29 of 45, one with its shape 32 → 38, both in one example 10 → 16; allowed questions shown a forbidden official example 22 → 5; query-agent user message median 2,426 → 2,220 tokens.

## 2. Scoreboard

### Whole pipeline (verdicts, time, tokens)

| Date | Configuration | 31 shared: verdicts | 198 dev | 198 holdout | p50 / p95 | Tokens |
|---|---|---|---|---|---|---|
| 16 Oct | stub agents, no model | 87% | | | 13 ms / 53 ms | 0 |
| 16 Oct | Qwen3.5-9B, prompts v1/v1, no codes | 90.3% | | | 2.8 s / 16.6 s | ~3,000 |
| 17 Oct 05:00 | **Merged system**: intent v4, query v2, cap 900, terminology index + value sets | 90.3%, 0 leaks | 91.3% (1 leak of 88, 12 of 61 allowed blocked) | **83.7%** (2 leaks of 29, 6 of 20 allowed blocked) | 3.1 s / 19.2 s (one at a time); 4.0 s / 14.5 s (4 at a time) | ~3,600 |
| 17 Oct 13:20 | **intent v4, query v4, cap 1,500**, index + value sets, context 3/6/5 | | 94.0% (1 leak of 88, 8 of 61 allowed blocked) | **91.8%** (2 leaks of 29, 2 of 20 allowed blocked) | 3.8 s / 13.7 s (4 at a time) | ~3,800 |
| 17 Oct 13:31 | as above, **2 examples instead of 3** (rejected) | | query eval only: queries that run 77% → 60%, allowed blocked 5 → 13, exact 3 → 1, query tokens 3,369 → 3,908 (more guard retries) | | | |
| 17 Oct 13:39 | query **v5** (v4 + speed rules incl. `DEFINE sql:select-option "order"`), rejected | | 93.3% (1 leak, 9 allowed blocked) | **81.6%** (2 leaks, 7 of 20 allowed blocked) | 3.7 s / 19.7 s | ~4,090 |
| 17 Oct 14:20 | v4 + **retry feedback carries the rejected query** (rejected) | | 94.0% | **85.7%** (2 leaks, 5 allowed blocked) | 3.8 s / 14.5 s | ~3,850 |
| 17 Oct 14:35 | v4 again, same configuration (**noise check**) | | 94.6% (1 leak, 7 allowed blocked) | **87.8%** (2 leaks, 4 of 20 allowed blocked) | 3.8 s / 20.2 s | ~3,820 |
| 17 Oct 14:45 | query **v6** (v4 + inference only on concrete codes, only the patterns the question needs; no DEFINE), rejected: no signal | | 96.0% (1 leak, 5 allowed blocked) | **89.8%** (2 leaks, 3 of 20 allowed blocked) | 3.7 s / 12.6 s | ~3,930 |
| 17 Oct 16:30 | **All fixes, run 1**: judge checks and repairs, guard policy, one retry per checker, cards 8 with code systems and value sets, retrieval shapes and aliases (FAILURE_ANALYSIS §4) | | 96.6% (0 leaks, 5 allowed blocked) | **87.8%** (2 leaks, 4 of 20 allowed blocked) | 4.3 s / 21.5 s | ~5,030 |
| 17 Oct 17:10 | All fixes, run 2 (noise check) | | 95.3% (0 leaks, 7 allowed blocked) | **91.8%** (2 leaks, 2 of 20 allowed blocked) | 4.5 s / 18.1 s | ~4,960 |
| 17 Oct 17:20 | **All fixes + keep the less bad attempt when the retry is not clean + GROUP BY on a SELECT alias accepted (run 3, the new default)** | | **97.3%** (0 leaks, 4 allowed blocked) | **93.9%** (2 leaks, 1 of 20 allowed blocked) | 4.1 s / 20.2 s | ~4,870 |
| 17 Oct 19:50 | **Round 2**: deterministic rewrites, hints built from the query, question checks, guard and retry policy, cards (run 1) | | **99.3%** (0 leaks, 1 allowed blocked) | 91.8% (2 leaks, 2 of 20 allowed blocked) | 4.6 s / 21.0 s | ~4,890 |
| 17 Oct 20:05 | Round 2 (run 2, noise check) | | **99.3%** (0 leaks, 1 allowed blocked) | 91.8% (2 leaks, 2 of 20 allowed blocked) | 4.3 s / 21.7 s | ~4,840 |
| 17 Oct 20:15 | Round 2 + `CONTEXT_DEV_REFERENCES=true` (the 45 dev reference queries as retrieval examples; dev numbers are contaminated, read the holdout only) | | 98.0% (0 leaks, 3 allowed blocked) | 91.8% (2 leaks, 2 of 20 allowed blocked) | 3.9 s / 24.0 s | ~4,620 |
| 17 Oct 20:26 | Round 2 + dev references (run 2, noise check; became the default) | | 95.3% (0 leaks, 7 allowed blocked) | 93.9% (2 leaks, 1 of 20 allowed blocked) | 4.0 s / 24.2 s | ~4,670 |
| 17 Oct 21:14 | **Qwen3.6-27B** (`cyankiwi/Qwen3.6-27B-AWQ-INT4`, 4-bit) **for both agents**, same pipeline: the default since 21:00 ([MODEL_HISTORY.md](MODEL_HISTORY.md)) | | **99.3%** (0 leaks, 1 allowed blocked) | **100%** (0 leaks, 0 of 20 allowed blocked) | 5.3 s / **17.9 s** | **~3,970** |
| 18 Oct 00:22 | + the express fixes (the default since 00:27); measured beside two other evals, so the times are not comparable | | 99.3% (0 leaks, 1 allowed blocked) | 100% (0 leaks, 0 of 20 allowed blocked) | (load) | ~3,950 |

**Noise.** Two runs of the same configuration (13:20 and 14:35) differ by 4 points on the holdout (2 of its 49 questions) and flip 9 of the 198 verdicts (7 dev, 2 holdout; dev net +1). Vllm samples at temperature 0 but batching makes the answers non-deterministic, and one holdout question is worth 2 points. So a single run cannot see a change smaller than about 5 holdout points: judge a change on all 198 and on *which* questions flipped (the runs page lists them), not on the holdout percentage alone, and run the final candidate twice before it becomes the default.

### Intent agent alone

| Date | Prompt | Model | Questions | Detected | False alarms | Right flag | Tokens | p50 |
|---|---|---|---|---|---|---|---|---|
| 17 Oct | v1 | Qwen3.5-9B | 73 | 90.5% | 0% | 91.9% | 314 | 1.3 s |
| 17 Oct | v2 | Qwen3.5-9B | 73 | 100% | 0% | 100% | 554 | 1.4 s |
| 17 Oct | v2 | Qwen3.5-9B | 198: dev / holdout | 96.6% / **93.1%** | 0% / **5%** | 94.0% / 89.7% | 558 | 1.6 s |
| 17 Oct | **v4** (default) | Qwen3.5-9B | 198: dev / holdout | 98.9% / **93.1%** | 1.6% / **5%** | 98.8% / 93.1% | 631 | 1.6 s |
| 17 Oct | v4 | Qwen3-Coder-30B-A3B (4-bit) | 198: dev / holdout | 96.6% / 93.1% | 1.6% / 10% | | 607 | 1.1 s |
| 17 Oct 21:02 | **v4** | **Qwen3.6-27B (4-bit), the default since 21:00** | 198: dev / holdout | **100% / 100%** | 1.6% / **0%** | 100% / 100% | 625 | 2.2 s |
| 17 Oct 14:05 | v5 (rejected) | Qwen3.5-9B | 198: dev / holdout | 97.7% / 93.1% | 0% / 5% | 96.4% | 685 | 1.8 s |

### Query agent vs reference SPARQL (47 questions, executed on dev)

| Date | Prompt | Model | Context | Cap | Exact | Runs (holdout) | First try | Allowed blocked | Query tokens | p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|---|
| 17 Oct | v1 | Qwen3.5-9B | no codes | 900 | 2 | 62% (60%) | 49% | 10 | 2,593 | 7.2 s / 18.1 s |
| 17 Oct | v2 | Qwen3.5-9B | no codes | 900 | 2 | 60% (73%) | 57% | 6 | 2,711 | 6.7 s / 17.2 s |
| 17 Oct | v2 | Qwen3.5-9B | index + value sets | 900 | 2 | 68% (80%) | 57% | 10 | 3,159 | 6.9 s / 19.0 s |
| 17 Oct | v3 | Qwen3.5-9B | index + value sets | 900 | 3 | 68% (73%) | 57% | 9 | 3,383 | 7.1 s / 17.1 s |
| 17 Oct | v2 | Qwen2.5-Coder-14B (4-bit) | index + value sets | 900 | 2 | 74% (67%) | 60% | 9 | 3,070 | 4.5 s / 10.9 s |
| 17 Oct | v3 | Qwen3-Coder-30B-A3B (4-bit) | index + value sets | 900 | 3 | 60% (53%) | 60% | 7 | 3,164 | **3.6 s / 7.5 s** |
| 17 Oct | v2 | Qwen3.5-9B, merged system | index + value sets | 900 | 2 | 64% (80%) | 57% | 10 | 3,143 | 7.0 s / 19.5 s |
| 17 Oct 13:00 | **v4** (default) | Qwen3.5-9B, merged system | index + value sets | **1,500** | **3** | **77% (87%)** | 57% | **5** | 3,369 | 6.4 s / 18.3 s |
| 17 Oct 13:34 | v5 (rejected) | Qwen3.5-9B, merged system | index + value sets | 1,500 | 4 (1 holdout) | 66% (80%) | 72% | 5 | 3,372 | 6.2 s / 11.5 s |
| 17 Oct 14:10 | **v4, 87 questions** (new baseline; 47 before) | Qwen3.5-9B, merged system | index + value sets | 1,500 | 4 (1 holdout) | 67% (68%) | 62% | 9 | 3,345 | 6.6 s / 16.0 s |
| 17 Oct 14:41 | v6, 87 questions (rejected) | Qwen3.5-9B, merged system | index + value sets | 1,500 | 4 (1 holdout) | 63% (64%) | 64% | 12 | 3,356 | 6.2 s / 15.7 s |
| 17 Oct 16:24 | **All fixes, run 1** | Qwen3.5-9B, merged system | cards 8 + code systems + value sets, aliases, team examples | 1,500 | 6 exact, **16 right numbers** | 70% (71%) | 30% | 8 | 7,093 | 9.1 s / — |
| 17 Oct 17:05 | All fixes, run 2 (noise) | same | same | 1,500 | 6 exact, 15 right numbers | 74% (68%) | 33% | 10 | 7,186 | 8.9 s / — |
| 17 Oct 17:15 | **All fixes + less bad attempt kept (run 3, default)** | same | same | 1,500 | **6 exact, 18 right numbers** | **78% (81%)** | 34% | **6** | 7,001 | 8.6 s / — |
| 17 Oct 19:47 | **Round 2** (rewrites, hints, question checks, guard and retry policy, cards), run 1 | same | cards without placeholders or the pseudo-identifier link, synonyms, value sets to 20 | 1,500 | **12 exact, 27 right numbers** (dev 20 of 56, holdout 7 of 31) | **86% (81%)** | 29% | **3** | 6,995 | 10.9 s / 27.0 s |
| 17 Oct 19:58 | Round 2, run 2 (noise) | same | same | 1,500 | 13 exact, 28 right numbers (dev 21, holdout 7) | 84% (77%) | 29% | 4 | 6,812 | 10.7 s / 26.7 s |
| 17 Oct 20:08 | Round 2 + `CONTEXT_DEV_REFERENCES=true` (dev contaminated: read the holdout) | same | + 45 dev references in the bank | 1,500 | 21 exact, 29 right numbers (dev 19, **holdout 10 of 31**) | 87% (**94%**) | 46% | **1** | 5,817 | 8.2 s / 18.3 s |
| 17 Oct 20:20 | Round 2 + dev references, run 2 (noise; became the default) | same | same | 1,500 | 22 exact, 30 right numbers (dev 19, holdout 10 of 31, official 1 of 11) | 85% (87%) | 45% | 4 | 5,913 | 8.7 s / 28.2 s |
| 17 Oct 20:34 | 5 retrieved examples instead of 3 (`CONTEXT_EXAMPLES=5`), rejected: no gain | same | 5 examples | 1,500 | 22 exact, 28 right numbers (dev 18, holdout 9 of 31) | 86% (84%) | 59% | 4 | 4,407 | 6.5 s / 24.8 s |
| 17 Oct 20:48 | **Model test: Qwen3.6-27B** (4-bit AWQ), holdout and official only | Qwen3.6-27B | same as 20:20 | 1,500 | holdout: 15 exact, **19 right numbers of 31**; official: 1 of 11 | holdout 94% | 81% | 0 | 3,394 | 9.5 s / 28.3 s |
| 17 Oct 20:58 | Model test: Qwen3.8-27B (4-bit AWQ), holdout and official only; not adopted | Qwen3.8-27B | same | 1,500 | holdout: 11 exact, 16 right numbers of 31; official: 1 of 11 | holdout 84% | 81% | 0 (1 official) | 3,386 | 9.3 s / 20.5 s |
| 17 Oct 21:07 | **Qwen3.6-27B, full run (the default since 21:00)** | Qwen3.6-27B | same | 1,500 | **34 exact, 48 right numbers** (dev 28, **holdout 19 of 31**, official 1 of 11) | **92% (94%)** | **83%** | **1** | **3,325** | **7.2 s / 21.9 s** |
| 17 Oct 21:16 | Qwen3.6-27B, holdout again (third measurement) | Qwen3.6-27B | same | 1,500 | holdout: 15 exact, 19 right numbers of 31 | holdout 94% | 81% | 0 | 3,394 | 8.1 s / 19.7 s |
| 17 Oct 21:32 | Thinking on for the query agent (temperature 0.6, top_p 0.95, top_k 20, cap 8,000; intent agent unchanged), holdout and official only; **rejected**: +1 question for 11× the latency | Qwen3.6-27B | same | 8,000 | holdout: 15 exact, 20 right numbers of 31 (5 answers cut off, in 4 questions); official: 1 of 11 | holdout 94% | 77% | 0 | ~8,500 per request | **90.7 s / 244 s** |
| 18 Oct 00:21 | Express fixes (0: official-03 scored as asked too; B: location codes say where they are used; 2: year-free ICD-10-GM/CHOP group key; 4: conditional count as BIND(IF); 1: the counts reference queries always show, as extra columns), measured beside the 198 verdicts and the jury view (20 requests at once on a GPU that decodes 8): not comparable | Qwen3.6-27B | + location context | 1,500 | 52 right numbers (dev 30, holdout 20 of 31, official 2 of 11); q-006 lost only because the 75 s deadline skipped its second judge retry | 92% | 86% | 1 | 3,303 | 21.7 s / 51.6 s (load) |
| 18 Oct 00:27 | **Express fixes, alone at concurrency 4: adopted** | Qwen3.6-27B | + location context | 1,500 | **34 exact, 53 right numbers** (dev 31, **holdout 20 of 31**, official **2 of 11**; as asked 3 of 11) | 92% (94%) | 85% | 1 | 3,304 | **7.2 s / 20.1 s** |
| 17 Oct 21:50 | Bank restored to its 3 team examples (team-ex-03 converts ages to years), holdout only | Qwen3.6-27B | bank 19 + 45 dev references | 1,500 | holdout: 15 exact, **19 right numbers of 31**: the same answers as at 21:16 | holdout 94% | 81% | 0 | 3,394 | run beside the next one |
| 17 Oct 21:50 | Best of 3 (`QUERY_CANDIDATES=3`, candidates at temperature 0.7, the judge keeps the least bad; greedy kept 27 times of 31), holdout only; **not adopted**: the judge ranks validity, not the numbers | Qwen3.6-27B | same | 1,500 | holdout: 14 exact, 18 right numbers of 31 | holdout 94% | **90%** | 0 | ~7,500 per request (vs ~4,000) | run beside the previous one |
| 17 Oct 21:40 | + 41 team examples, each verified on dev by a second query written blind (bank 105); **rejected**: the blind holdout falls 19 → 13. The examples are kept aside in `data/team_examples_candidates.jsonl`, which nothing loads | Qwen3.6-27B | bank 60 + 45 dev references | 1,500 | 31 exact, 45 right numbers (dev 29, **holdout 13 of 31**, official 3 of 11: contaminated, 11 of the examples are variants of the official questions); verdicts dev 100% / holdout 98% | 91% (90%) | 85% | 1 | 3,387 | 7.6 s / 19.9 s |

Reading: the context (codes) made more queries run; the prompt v4 halved the blocked allowed questions; a bigger model did not change the exact-result rate, only the speed. Prompt v6 is within the noise of v4 on the verdict set (dev +1.4 / +2.0 points, holdout between the two v4 runs) and slightly worse on the query eval (queries that run 67% → 63%, blocked 9 → 12; 10 dev questions better, 12 worse): no signal, v4 stays.

**The fixes (17 Oct evening).** "Right numbers" = exact + `exact_numbers` (the reference's numbers with other labels, rounding or layout; v4's 4 is its exact count, the outcome did not exist before 16:00). Three runs of the fixed system against the two v4 runs: queries that run 67% → 70 / 74 / 78% (holdout 68% → 71 / 68 / 81%), right numbers 4 → 16 / 15 / 18, Virtuoso errors 29 → 26 / 23 / 19, allowed questions blocked 9 → 8 / 10 / 6; on the verdict set, dev 94.0 / 94.6% → 96.6 / 95.3 / 97.3% and holdout 91.8 / 87.8% → 87.8 / 91.8 / 93.9%, with the dev leak gone (gen-106) and the five dev questions wrong in both v4 runs (gen-077, gen-096, gen-106, gen-119, intent-035) right in run 3. The price: the judge now rejects 57 of 87 first tries (29 for syntax, the model's own; 62% → 34% "first try" is the stricter judge, not a worse model), so query tokens double (3,345 → ~7,000 per question with the retry) and p50 goes from 6.6 to 8.6 s on the query eval, 3.8 to 4.1 s on the pipeline. Run 3's extra over run 1 is a policy, not a prompt: a retry that is still not clean is a fresh query, worse than the first answer as often as better (13 better, 10 worse of 36 dev rows), so the pipeline keeps the less bad attempt by the judge's own severity (`nl2sparql.judge.severity`): 11 of 54 judge retries in run 3 kept the first try. The first-try rate is now a different metric (judge strictness) and no longer comparable with the rows above. The failures of the v4 configuration are analyzed case by case in [FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md), with the fixes ranked by the cases they cover; most of them are code (judge checks, guard policy, context builder), not prompt wording.

**Round 2 (17 Oct night).** A second analysis of run 3's 45 wrong dev answers found that a judge hint buys a whole new query, which comes back worse as often as better, while the model's mistakes repeat deterministically (the same first try in 3 of 3 runs). So shapes with exactly one correct form are now repaired by the judge with no model call (`judge_checks/rewrites_scope.py`, `rewrites_aggregation.py`, the syntax repairs of `judge_checks/syntax.py`), the remaining hints are built from the query's own variables, records and codes, the judge reads the question (the coded concepts it names, the record count it asks for), the guard's hints say what to write and a parse error's retry carries the judge's diagnosis, the judge may retry once more after a retry spent on syntax alone, the guard's retry keeps the less bad attempt, and the cards show no copyable placeholder nor the pseudo-identifier link. Replayed offline on run 3's dev answers, before any new model call: 8 of the 45 wrong answers become right (q-020, q-022, q-042, q-050, q-054, q-060, q-064, q-073; dev 11 → 19 of 56), one goes from wrong to empty (q-055); 0 hits on the 16 official, 3 team and 45 dev reference queries. The measured runs: on dev, right numbers 11 → 20 / 21 of 56 (36–38%; 26 questions right in both runs), queries that run 78% → 86 / 84%, allowed questions blocked 6 → 3 / 4, Virtuoso errors 19 → 12 / 14, verdicts 97.3% → 99.3% in both runs (1 allowed question blocked, 0 leaks). On the holdout the rewrites alone change little: right numbers 7 → 7 of 31, queries that run 81% → 81 / 77%, verdicts 93.9% → 91.8% (the noise band). The dev references as examples (lever B) are what reach the holdout: right numbers 7 → **10 of 31**, queries that run 81% → **94%**, allowed questions blocked 1 → 2, with fewer tokens (6,995 → 5,817) and a lower p50 (10.9 → 8.2 s) because the model needs fewer retries when it sees a neighbouring reference; verdicts 91.8% either way. One run so far; a second one decides whether the flag becomes the default. On the 198-question verdict set, v4 with the 1,500 cap took the holdout from 83.7% to 91.8%: 7 dev questions fixed, 3 broken (#08 unparseable, #10 and one more grouped by a record node), retries 33 → 24, cut-off answers 10 → 4. Holdout exact results are 0 in every run: the hard questions are not solved by any configuration yet.

**The model test (17 Oct, 20:35–21:20).** With the pipeline now able to use a stronger model, we tested the strongest models that fit the RTX 5090 (32 GB) in 4 bits, with everything else unchanged. Qwen3.6-27B took the blind holdout from 10 to **19 of 31** right numbers, the same in three runs (temperature 0), with fewer Virtuoso errors (holdout 4 → 2) and a judge that accepts 81% of first tries instead of 48%; because it needs fewer retries it is also faster (p50 8.7 → 7.2 s on the 87 questions) and cheaper (median query tokens 5,913 → 3,325). The newer Qwen3.8-27B gave 16 of 31 and 5 Virtuoso errors. Qwen3.6-27B is the default since 21:00 for both agents; on the 198 verdicts it scores 99.3% dev / 100% holdout with 0 leaks. The official examples stay at 1 of 11 with every model: their references make choices the questions do not state. Reasons, rejected options and the procedure are in [MODEL_HISTORY.md](MODEL_HISTORY.md).

**Two levers that did not survive the 27B (17 Oct, 21:30–21:50).** Thinking mode gained one holdout question (20 of 31, within the sampling noise) for a p50 of 91 s instead of 8 s: off. Forty-one new team examples, each confirmed on dev by a second query written blind, took the holdout from 19 to 13 of 31 while dev went 28 → 29 and the official examples 1 → 3 (contaminated: 11 of them are variants of those questions). Our reading: BM25 now ranks the long, official-style team questions above the dev reference queries, which come from the same distribution as the holdout and were what moved it before (7 → 10 with the 9B). More examples are not better examples; the bank keeps its 3 team examples (team-ex-03 now converts every age to years first, since dev stores ages in six units) and the 41 wait in `data/team_examples_candidates.jsonl` for a ranking that puts the dev references first.

**The express fixes (18 Oct, 00:00–00:30).** A diagnosis of the official examples that fail when their own example is hidden found that 6 of the 10 failures come from choices the organizers' references make and the questions do not state (official-03 even contradicts its question), and 4 from model errors that repeat in patterns. Five deterministic fixes, each built in its own worktree with tests and replayed on the saved answers (0 right answers lost), were merged: a year-free group key for ICD-10-GM/CHOP codes, conditional counts as `BIND(IF(...))`, the counts reference queries always show as extra columns, location codes that say where they are used ("How many patients live in India?" went from 0 to the right 22), and official-03 scored as asked too. Alone at concurrency 4: right numbers 48 → 53 of 87, blind holdout 19 → **20 of 31**, official 1 → 2 of 11 (3 as asked), dev 28 → 31 with none lost, the same 34 exact, the same verdicts, the same p50 (7.2 s). **Measure alone:** the first run shared the GPU with two other evals, 20 requests at once on a server that decodes 8, so the 75 s deadline skipped second judge retries (q-006 lost) and the times tripled.

### Query time on 100,000 patients

| Date | Queries | Result |
|---|---|---|
| 17 Oct, pilot | 5 generated (v2) vs their references, 60 s server limit | References #04, #05, #06 pass 60 s (they took 28 min to over 30 min on this machine). Generated #07 passes 60 s where the reference takes 1.2 s. #04, #06, #09 are fast but return a wrong or empty result. |
| 17 Oct 13:30 | 36 v4 queries that ran on dev, 1 warm-up + 2 timed runs, 60 s server limit | 22 pairs completed on both sides: median generated 0.3 s vs reference 0.5 s, ratio 0.9. 11 reference queries pass 60 s (#01, #02, #04, #05, #06, #10, q-014, q-017, q-025, q-028, q-033); 6 generated pass 60 s, 4 of them where the reference also does. Generated slow where the reference is fast: q-018 and q-026 time out (reference 3.8 s and 0.5 s); spec-01 takes 1.4 s for a 0.01 s reference (×142) with the right result; #09, q-009, q-036 are ×3–4. 15 fast generated queries return a wrong or empty result on dev. |

| 17 Oct 14:00 | 31 v5 queries that ran on dev, same protocol | Worse: 8 generated time-outs (v4: 6), including q-013, q-014, q-015, q-017 and q-020 which were fine with v4; spec-01 still 1.5 s. Forcing the join order with `DEFINE sql:select-option "order"` only helps when the written order is good, and the model does not order patterns reliably. |
| 17 Oct 18:05 | The 4 run-1 queries the restricted reorder pass rewrites (`QUERY_REORDER`, now on by default: one typed code in the first GRAPH block, no UNION/MINUS/sub-select, `hasCode` first in the linking statement), plain runs, 30 s deadline | Identical rows on dev for all four. spec-01 0.01 → 0.01 s, q-015 0.19 → 0.26 s, q-019 0.00 → 0.00 s, **q-021 0.44 → 0.06 s** (reference 1.65 s). Small, safe, on. |
| 17 Oct 17:50 | **61 queries of the fixed system (run 1)** that ran on dev, plain runs, 30 s client deadline, 1 timed run, 44 reference timings reused from earlier reports | 42 of 61 pairs complete on both sides: median generated **0.2 s vs reference 0.4 s**, ratio 0.7. 10 generated queries pass 30 s (official-03, q-017, q-023, q-029, q-033, q-047, q-055, q-063, q-064, q-068; v4 had 6 of 36 past 60 s), 6 are over twice the reference (q-011, q-031, q-058, q-059, q-062, q-070); 14 container restarts, 48 minutes. spec-01 1.42 → 0.01 s without the reorder pass (the new context makes the model write it code-first), q-006 / q-009 / q-016 2–10× faster; q-017 (1.3 s) and q-023 (0.2 s) now overrun; official-04 0.00 → 9.4 s (a different, still wrong query). Of the 51 generated queries that complete, 36 are wrong, empty or right-count-wrong-values on dev: speed is not the bottleneck, correctness is. |
| 17 Oct 21:55 | **80 queries of Qwen3.6-27B (full run, 21:07)** that ran on dev, same protocol, reference timings reused | 58 of 80 pairs complete on both sides: median generated **0.3 s vs reference 0.5 s**, median ratio 0.9; 10 generated and 19 reference queries time out at 30 s; **6** generated queries slower than their reference (9B: 9) (`perf-20261017-215503.json`) |
| 17 Oct 21:11 | 74 queries of round 2 + dev references (Qwen3.5-9B, run 2) that ran on dev, plain runs, 30 s client deadline, 1 timed run, 67 reference timings reused | 54 of 74 pairs complete on both sides: median generated 0.3 s vs reference 0.6 s, median ratio 0.8; 10 generated and 16 reference queries time out at 30 s; 9 generated queries slower than their reference (`perf-20261017-211133.json`) |
| 17 Oct 15:30 | 55 v6 queries that ran on dev (87-question set), same protocol | 30 pairs complete on both sides, median generated 0.3 s vs reference 0.5 s, 16 generated time-outs. Not comparable with the 47-question rows above (more and harder questions), and part of the time-outs are artifacts of the anytime `timeout` parameter on OPTIONAL + inference shapes (see FAILURE_ANALYSIS §3.6); the harness now times plain runs (17 Oct 16:00); re-run before comparing. |
| 17 Oct 14:30 | The 47 v4 queries after the deterministic reorder pass (`QUERY_REORDER`, `nl2sparql.sparql.order`: code patterns first + `DEFINE sql:select-option "order"`), same protocol | On dev nothing changes except q-013, which Virtuoso refused before and now runs (13 queries reordered, 34 untouched apart from the DEFINE). On perf, mixed: spec-01 1.42 → 0.02 s (reference 0.01), official-01 4.5 → 2.5 s, q-009 1.9 → 1.3 s, but q-015 0.19 → 9.0 s and q-017 1.3 → 8.8 s. Forcing the order wins when code-first is the right plan and loses when it is not; the pass stays off until it applies only to the shapes where it wins. |

A fast query with a wrong result scores nothing: read this table together with the exact-result column above.

### Load

| Date | Setup | Result |
|---|---|---|
| 17 Oct | 32 requests, 8 at a time, API on the run machine, Qwen3.5-9B | 32 × HTTP 200, p50 3.7 s, p95 11.2 s, 1.33 requests/s, 2,873 tokens on average |
| 17 Oct 23:58 | The same 32 requests (the 31 shared questions), 8 at a time, Qwen3.6-27B on the RTX 5090, API on its own port | 32 × HTTP 200, p50 **8.9 s**, p95 **19.1 s**, max 22.4 s, **0.65 requests/s**, 3,617 tokens on average. One request at a time the 27B answers in about 5 s; eight at once saturate the GPU, which decodes a dense 27B at half the 9B's rate. A faster GPU for the same model would cut this without touching quality |

## 3. Where the time and the tokens go

A request without retry, Qwen3.5-9B, merged system:

| Step | Time | Tokens | Note |
|---|---|---|---|
| Input check, context, judge, guard | milliseconds | 0 | Code |
| Intent agent | 1.6 s, **hidden**: runs in parallel with the query agent | ~630 | Prompt v4 is 556 of them |
| Query agent | ~2.7 s | ~2,300 prompt + answer | 3 examples (~1,100), 6 schema cards, 5 code candidates, 439-token system prompt |
| Retry (judge or guard) | +3 to +10 s | +~3,200 | In about a third of requests. The single biggest lever for time and tokens |

## 4. Levers, ranked by expected gain

**Time**
1. **Fewer retries.** Better judge hints and the Virtuoso-only checks in code (module D); shorter, simpler queries from the prompt (module B). ~~Showing the model its rejected query on the retry~~: measured on 17 October and rejected: the model copies the broken query instead of fixing it (judge retries ending valid 8/18 → 3/19, queries that run 67% → 48%).
2. ~~A faster model for the query agent~~: done differently. Qwen3-Coder-30B-A3B (4-bit) answered in 3.6 s / 7.5 s against 6.9 s / 19 s in the morning, with similar quality but a worse intent agent. In the evening Qwen3.6-27B (4-bit) replaced the 9B for both agents: better and, through fewer retries, faster (p50 8.7 → 7.2 s on the 87 questions). See [MODEL_HISTORY.md](MODEL_HISTORY.md).
3. ~~Two examples instead of three~~: measured on 17 October and rejected. With 2 examples fewer queries run (77% → 60%), more allowed questions are blocked (5 → 13) and the retries cost more tokens than the example saved. The examples carry the house style; keep 3.

**Verdicts**
1. Re-run the 198 set with prompt v4: fewer allowed requests blocked is expected.
2. ~~The intent false alarm on counts that mention identifiers~~: prompt v5 fixed it on dev but changed nothing on the holdout and cost 54 tokens per call; rejected, v4 stays.
3. The two holdout leaks and the retry hint for "grouping by ?code" (module D).

**Query time on 100,000 patients**
1. Read the perf harness report: which generated queries time out where the reference does not, and what shape they have (`OPTIONAL`, subqueries, pattern order).
2. Turn the findings into prompt rules and, where a rewrite is mechanical, into judge fixes.

**Tokens**
1. Shorter schema cards: only the properties a question needs.
2. No code candidates when the value sets in the cards already cover the question.

## 5. Protocol for a change

**The short version:** `python eval/run_all.py --label "<what you changed>"`, then open <http://localhost:8080/agents/runs>, pick the previous run as A and yours as B, and read the metric deltas and the questions that got fixed or broken. Keep the change only if the holdout does not get worse. Then paste the numbers here and in the pull request.

### Steps

1. Branch from `main`.
2. Change one thing.
3. Run the evals that the change can affect (section 1), with the GPU.
4. Compare with the last row of the scoreboard in the console. Keep the change only if the holdout numbers do not get worse.
5. Commit with the numbers in the message, add a row here, open the pull request with the eval summary.
