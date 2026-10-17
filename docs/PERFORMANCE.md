# Performance log

One place for every measured run: the configuration behind it, the numbers, and what moved. Update it after every change you measure, and paste the same numbers in your pull request.

The jury scores three things: **time per request**, **verdict correctness** against their labels, and **execution time of our query on their machine** (average of 10 isolated runs). Tokens and model size are minor criteria.

## 1. How we measure

| Eval | Command | Questions | Measures | Needs |
|---|---|---|---|---|
| Whole pipeline, shared set | `python eval/run_eval.py --execute` | 31 (16 official + 15 team) | Verdicts, latency, tokens per step, retries | GPU, dev Virtuoso |
| Whole pipeline, labeled set | `python eval/run_eval.py --intent-cases --concurrency 4` | 198 (31 + 167 intent cases) | **The verdict metric**, split dev / holdout | GPU |
| Intent agent alone | `python eval/run_intent_eval.py --concurrency 4` | 198 | Detection, false alarms, right flag, tokens, latency | GPU |
| Query agent vs reference SPARQL | `python eval/run_query_eval.py --concurrency 4` | 87 (11 official + 76 verified; 47 until 17 Oct 14:00) | Exact result, runs on Virtuoso, first-try validity, blocked allowed, query tokens | GPU, dev Virtuoso |
| Query time on 100,000 patients | `python eval/time_on_perf.py eval/reports/query-<date>.json --runs 2` | The queries of a query report | Seconds vs the reference query, timeouts | perf Virtuoso |
| Load | `python -m nl2sparql.load_test --url http://localhost:8080 --questions eval/questions.jsonl --requests 32 --concurrency 8 --require-llm` | 15 | Failures, p50/p95, throughput | GPU, running API |

**Rules.**
- **Correctness on dev, timing on perf.** The dev database (2,000 patients) answers reference queries in seconds; on perf (100,000) the official references take minutes or hours, so only timing runs there, with Virtuoso's server-side `timeout`.
- **Dev and holdout.** Questions marked `holdout` are a blind exam: never study them one by one or tune anything on them. A change is kept only if the holdout does not get worse.
- **One change per run.** Prompt version, context size, model, answer cap: change one, measure, commit with the numbers.
- **Reports** land in `eval/reports/` (git-ignored) and show up in the agents console, <http://localhost:8080/agents>, under **Measurements**.
- Latency is comparable only between runs at the same concurrency. Concurrency 4 adds queueing on an 8-sequence vLLM.

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
| next | query v6: v4 + "inference only on concrete codes" + "only the patterns the question needs", without the DEFINE | | | | | |

### Intent agent alone

| Date | Prompt | Model | Questions | Detected | False alarms | Right flag | Tokens | p50 |
|---|---|---|---|---|---|---|---|---|
| 17 Oct | v1 | Qwen3.5-9B | 73 | 90.5% | 0% | 91.9% | 314 | 1.3 s |
| 17 Oct | v2 | Qwen3.5-9B | 73 | 100% | 0% | 100% | 554 | 1.4 s |
| 17 Oct | v2 | Qwen3.5-9B | 198: dev / holdout | 96.6% / **93.1%** | 0% / **5%** | 94.0% / 89.7% | 558 | 1.6 s |
| 17 Oct | **v4** (default) | Qwen3.5-9B | 198: dev / holdout | 98.9% / **93.1%** | 1.6% / **5%** | 98.8% / 93.1% | 631 | 1.6 s |
| 17 Oct | v4 | Qwen3-Coder-30B-A3B (4-bit) | 198: dev / holdout | 96.6% / 93.1% | 1.6% / 10% | | 607 | 1.1 s |

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

Reading: the context (codes) made more queries run; the prompt v4 halved the blocked allowed questions; a bigger model did not change the exact-result rate, only the speed. On the 198-question verdict set, v4 with the 1,500 cap took the holdout from 83.7% to 91.8%: 7 dev questions fixed, 3 broken (#08 unparseable, #10 and one more grouped by a record node), retries 33 → 24, cut-off answers 10 → 4. Holdout exact results are 0 in every run: the hard questions are not solved by any configuration yet.

### Query time on 100,000 patients

| Date | Queries | Result |
|---|---|---|
| 17 Oct, pilot | 5 generated (v2) vs their references, 60 s server limit | References #04, #05, #06 pass 60 s (they took 28 min to over 30 min on this machine). Generated #07 passes 60 s where the reference takes 1.2 s. #04, #06, #09 are fast but return a wrong or empty result. |
| 17 Oct 13:30 | 36 v4 queries that ran on dev, 1 warm-up + 2 timed runs, 60 s server limit | 22 pairs completed on both sides: median generated 0.3 s vs reference 0.5 s, ratio 0.9. 11 reference queries pass 60 s (#01, #02, #04, #05, #06, #10, q-014, q-017, q-025, q-028, q-033); 6 generated pass 60 s, 4 of them where the reference also does. Generated slow where the reference is fast: q-018 and q-026 time out (reference 3.8 s and 0.5 s); spec-01 takes 1.4 s for a 0.01 s reference (×142) with the right result; #09, q-009, q-036 are ×3–4. 15 fast generated queries return a wrong or empty result on dev. |

| 17 Oct 14:00 | 31 v5 queries that ran on dev, same protocol | Worse: 8 generated time-outs (v4: 6), including q-013, q-014, q-015, q-017 and q-020 which were fine with v4; spec-01 still 1.5 s. Forcing the join order with `DEFINE sql:select-option "order"` only helps when the written order is good, and the model does not order patterns reliably. |

A fast query with a wrong result scores nothing: read this table together with the exact-result column above.

### Load

| Date | Setup | Result |
|---|---|---|
| 17 Oct | 32 requests, 8 at a time, API on the run machine, Qwen3.5-9B | 32 × HTTP 200, p50 3.7 s, p95 11.2 s, 1.33 requests/s, 2,873 tokens on average |

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
1. **Fewer retries.** Better judge hints and the Virtuoso-only checks in code (module D); shorter, simpler queries from the prompt (module B).
2. **A faster model for the query agent.** Qwen3-Coder-30B-A3B (4-bit) answered in 3.6 s / 7.5 s against 6.9 s / 19 s, with similar quality but a worse intent agent. The code supports one model per agent (`LLM_INTENT_*`): the 9B for the intent agent, the 30B for queries, on a second GPU instance.
3. ~~Two examples instead of three~~: measured on 17 October and rejected. With 2 examples fewer queries run (77% → 60%), more allowed questions are blocked (5 → 13) and the retries cost more tokens than the example saved. The examples carry the house style; keep 3.

**Verdicts**
1. Re-run the 198 set with prompt v4: fewer allowed requests blocked is expected.
2. The intent false alarm on counts that mention identifiers (module B).
3. The two holdout leaks and the retry hint for "grouping by ?code" (module D).

**Query time on 100,000 patients**
1. Read the perf harness report: which generated queries time out where the reference does not, and what shape they have (`OPTIONAL`, subqueries, pattern order).
2. Turn the findings into prompt rules and, where a rewrite is mechanical, into judge fixes.

**Tokens**
1. Shorter schema cards: only the properties a question needs.
2. No code candidates when the value sets in the cards already cover the question.

## 5. Protocol for a change

1. Branch from `main`.
2. Change one thing.
3. Run the evals that the change can affect (section 1), with the GPU.
4. Compare with the last row of the scoreboard in the console. Keep the change only if the holdout numbers do not get worse.
5. Commit with the numbers in the message, add a row here, open the pull request with the eval summary.
