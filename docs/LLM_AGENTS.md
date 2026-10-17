# LLM agents: developer guide

For whoever works on module B: the intent agent, the query agent, their prompts and their models. Read [DATA.md](DATA.md) first if you will touch the query agent's prompt. Nothing in this guide needs a GPU, except the steps that say so. Those run on the GPU instance and need its owner's OK (Santiago).

## Start here

| What | What it gives you |
|---|---|
| This guide | How the agents fit in the pipeline, configuration, what every request records, how to measure, how to change a prompt or a model, known issues, backlog |
| <http://localhost:8080/agents> | The agents console: configuration of both agents, a prompt preview (no model call), a run of one question showing what each agent did, and all saved eval reports |
| `eval/run_intent_eval.py` | The intent agent alone: detection rate, false alarms, invalid JSON, tokens. One short call per question |
| `eval/run_eval.py` | The whole pipeline, with tokens, time and retry reasons per agent |
| `src/nl2sparql/agents/prompts.py` | The prompts, by version |
| `tests/test_agents_and_judge.py`, `tests/test_pipeline_and_api.py`, `tests/test_agent_settings.py`, `tests/test_console.py` | Where agent tests live; add yours there |

First task: backlog item 1 in [section 9](#9-backlog-for-module-b-most-useful-first).

## 1. What the two agents do

```
request ─► input check ─┬─► intent agent (LLM, JSON) ───────────────────────────┐
                        │                                                        ▼
                        └─► context ─► query agent (LLM) ─► judge ─────► compliance guard ─► answer
                                         ▲                    │                  │
                                         └── one retry each, with feedback from ─┘
                                             the judge or the guard
```

- **The intent agent** reads only the request text. It answers in JSON with five flags (`asks_for_identifiers`, `single_patient`, `individual_rows`, `whole_graph`, `not_a_select`), a reason and a suggested rephrase. It runs in parallel with the context and the query agent, so its time is hidden: the total time is about the query agent's time. After the flags come back, `src/nl2sparql/intent_check.py` clears `asks_for_identifiers` on a pure counting request ("how many ...", "count ...", "what share ...") that carries no reveal cue (which, who, list, show, give, their, names, each, per patient, ...); no other flag is touched, and the pipeline records `intent_flag_cleared` in its diagnostics (intent-035, the only intent false alarm on dev; 0 leaks on the 149 dev rows).
- **The query agent** gets the request and the context (3 similar examples, schema cards, code candidates) and writes one SPARQL query. It translates literally, forbidden parts included, so the guard can judge them.
- **The judge and the guard are code, not models** (module D, see [GUARD.md](GUARD.md)). The judge repairs what needs no model (prefixes, three syntax mistakes, inference on a variable class, a date plus a duration, a typed value-set entry), checks syntax with hints that name the real error, terms absent from the data, and the three families of checks in `judge_checks/` (variable scope, semantics, schema). The guard applies the five rules to the query and combines them with the intent flags.

**Retry rules.** The judge and the guard each have one retry (`MAX_RETRIES` per checker), and the judge gets one more when its first retry was spent on syntax errors alone (`judge x2`), so the query agent is called at most four times per request, inside `REQUEST_TIMEOUT_SECONDS`:
- **The judge** asks for it when the query is invalid. The orchestrator first waits for the intent result, because a flagged request gets no retry at all.
- **The guard** asks for it only when the request is not flagged and the query breaks a rule.
- **A flagged request** never gets a retry, from the judge or from the guard.

The retry is decided by code, not by the agents. The judge or the guard finds the problem, and the orchestrator calls the query agent again. The query agent only sees the feedback added to its prompt. Module B's part of the retry is how well the model corrects itself and how many tokens that costs.

**Stand-ins.** With `NL2SPARQL_BACKEND=stub`, no model is used:
- `HeuristicIntentAgent` applies keyword rules.
- `ExampleQueryAgent` copies the SPARQL of the most similar example.

They only let the pipeline run end to end without a GPU.

## 2. Code map

| File | Role |
|---|---|
| `agents/base.py` | The two interfaces (shared with the team): `classify(text) -> (flags, tokens)` and `generate(text, context, feedback) -> (sparql, tokens)` |
| `agents/intent_llm.py` | `LLMIntentAgent`. `classify_detailed()` returns an `IntentReply`: flags, tokens, raw answer, `valid_json`, `finish_reason`, `fallback`. When the model fails or its answer is unusable, the keyword rules answer instead (plan B) |
| `agents/query_llm.py` | `LLMQueryAgent`. `generate_detailed()` returns a `QueryReply`: SPARQL, tokens, raw answer, `finish_reason`. `extract_sparql()` takes the query out of a code fence |
| `agents/prompts.py` | The prompts by version (`INTENT_PROMPTS`: v1, v2, v3; `QUERY_PROMPTS`: v1) |
| `agents/intent_heuristic.py`, `agents/query_examples.py` | The stand-ins |
| `agents/console.py`, `agents/console.html` | The `/agents` page and its JSON routes |
| `llm.py` | OpenAI-compatible client: temperature 0, JSON mode on request, extra request fields, reports `finish_reason` |
| `config.py` | `Settings.agent("intent" \| "query")` resolves each agent's settings, with the fallbacks below |
| `factory.py` | Builds one client per agent, with the prompt version from the settings |
| `orchestrator.py` | Runs both agents and records tokens, time and diagnostics per step |

The orchestrator uses `classify_detailed()` and `generate_detailed()` when an agent has them. That lets the LLM agents report more without changing the shared interfaces in `base.py`.

## 3. Configuration

Settings come from environment variables (README §7). Each agent has its own:

| Setting | Intent agent | Query agent |
|---|---|---|
| Model | `LLM_INTENT_MODEL`, falls back to `LLM_QUERY_MODEL` | `LLM_QUERY_MODEL` |
| Server | `LLM_INTENT_BASE_URL`, falls back to `LLM_BASE_URL` | `LLM_BASE_URL` |
| API key | `LLM_INTENT_API_KEY`, falls back to `LLM_API_KEY` | `LLM_API_KEY` |
| Extra request fields | `LLM_INTENT_EXTRA_BODY`: unset inherits `LLM_EXTRA_BODY`, `'{}'` sends none | `LLM_EXTRA_BODY` |
| Answer cap | `LLM_INTENT_MAX_TOKENS` (200) | `LLM_QUERY_MAX_TOKENS` (1500) |
| Prompt version | `INTENT_PROMPT_VERSION` (v4) | `QUERY_PROMPT_VERSION` (v4) |
| JSON mode | on | off |

Both agents also share `LLM_TIMEOUT_SECONDS` and `LLM_VERIFY_TLS`, and the temperature is always 0. The console shows the resolved values, with secrets hidden.

**Qwen3.x needs thinking off**: `LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'`. Otherwise it spends its whole answer budget thinking and returns nothing.

## 4. What every request records

`PipelineResult` carries three dictionaries. The judged API (`POST /nl-to-sparql`) returns only the four contract fields. The request log (`logs/requests.jsonl`), the demo, `/agents/run` and the eval reports show everything.

- **`tokens_by_step`**: `intent`, `query`, `query_retry`. `tokens_consumed` is their sum.
- **`timings_ms`**: `intent`, `context`, `generate`, `generate_retry`, `guard`, `guard_retry`, `total`.
- **`diagnostics`**: only the keys that apply.

| Key | Meaning |
|---|---|
| `retry_reason` | `judge`, `judge x2`, `guard`, `judge+guard` or `judge x2+guard`: who asked for a retry (`x2`: the judge's extra call after a retry spent on syntax errors alone) |
| `first_try_sparql` | The first answer; when the judge asked twice, the first retry is in `first_retry_sparql`; when both checkers retried, the retry the guard rejected is in `rejected_retry_sparql` |
| `kept_first_try`, `kept_first_retry`, `worse_retry_sparql` | A judge retry that was worse than the attempt before it was dropped: its errors, and the dropped answer |
| `guard_kept_first_try`, `guard_worse_retry_sparql` | The guard's retry still broke a rule or did not parse and was worse by the judge's severity, so the attempt the guard rejected stays the answer |
| `judge_retry_skipped` | `deadline`: the judge's extra call would not have fitted before `REQUEST_TIMEOUT_SECONDS` |
| `judge_repairs` | What the judge fixed without the model (`GROUP BY true` dropped, a prefix rewritten, ...) |
| `intent_flag_cleared` | `asks_for_identifiers` was cleared by the counting-request post-check |
| `judge_errors_first_try`, `judge_errors_first_retry`, `guard_violations_first_try` | Why an attempt was rejected: the first try by the judge, the first retry by the judge (before its extra call), the attempt the guard saw first |
| `judge_errors_final` | The judge's errors on the answer returned; absent when it is clean |
| `cut_off` | Steps whose answer hit the answer cap: `intent`, `query`, `query_retry` |
| `intent_invalid_json` | The intent answer was not the expected JSON |
| `intent_fallback` | The keyword rules gave the flags instead of the model: `error` (the call failed), `invalid_json` or `cut_off` |

**Plan B for the intent agent.** The verdict needs the flags. If the intent agent raises an exception, the orchestrator fails the whole request with HTTP 503 (module A's rule). To avoid that, `LLMIntentAgent` falls back to the keyword rules (`HeuristicIntentAgent`: no tokens, instant) in three cases:
- the model call fails (server down, timeout);
- the answer is not the expected JSON;
- the answer was cut off.

The keyword rules detect less (74% on the 73 intent questions), so a fallback is a safety net, not a substitute.

When the query agent's answer was cut off and the judge or the guard rejects it, the retry feedback starts with *"Your previous answer was cut off at the length limit. Write a shorter query and no explanation."*

## 5. Measuring

### The console

Run the API (`python -m nl2sparql --port 8080`) and open <http://localhost:8080/agents>:

| Part | Calls a model? |
|---|---|
| Configuration of both agents, with their system prompts | No |
| **Preview prompts**: the exact messages each model would receive for a question, with token estimates | No |
| **Run this question**: the whole pipeline. Shows the flags, tokens and time per agent, the retry and its first try, the rules broken and the final query | Yes, with `NL2SPARQL_BACKEND=llm` |
| **Measurements**: every report in `eval/reports/`. Click a row to see each question, problems first | No, it reads files |
| **Check servers**: asks each server which models it serves | One small request per server |

### The evals

```bash
python eval/run_intent_eval.py --concurrency 4   # the intent agent alone: 198 short calls, ~2 min
python eval/run_intent_eval.py --shared-only     # only the 31 questions below
python eval/run_eval.py               # the whole pipeline, per-agent numbers in "per_agent"
python eval/run_eval.py --execute     # also runs compliant queries on the dev Virtuoso
```

**Questions:** both evals use the 16 official examples and the 15 team questions in `eval/questions.jsonl` (31 in total). The intent eval adds the 167 cases in `eval/intent_questions.jsonl`, by group:
- paraphrases of each rule;
- traps, where an allowed ask is followed by a forbidden one;
- prompt injection, in both directions;
- allowed look-alikes and realistic research questions;
- gray zones, labeled with the guard's current assumptions (GUARD.md §6);
- style variants (typos, telegraphic, rambling, non-native English);
- German, French and Italian.

That makes 198 questions. 125 of them were written by an agent that never saw the intent prompts.

**Splits.**
- **dev** (149 questions): may be studied one by one to improve a prompt.
- **holdout** (49 questions): the blind exam. The eval reports it only as aggregate numbers (`by_split`) and leaves its ids out of every list.

Do not tune a prompt on holdout questions, or the exam stops measuring how the prompt does on unseen requests like the jury's.

**Reports:** `eval/reports/` is git-ignored. Every report records the models, prompt versions and answer caps it ran with, so reports stay comparable.

**Intent eval metrics:**

| Metric | Meaning | Better |
|---|---|---|
| Detection | Forbidden questions that raised a flag | Higher |
| False alarms | Allowed questions that raised a flag | Lower |
| Right flag | Questions naming a rule that raised that rule's flag | Higher |
| Invalid JSON, cut off, rules fallback | Unusable answers, and how often the keyword rules had to answer | 0 |
| Tokens, latency | Cost of one call | Lower |

### Baseline (2026-10-17)

| | Stub | Qwen3.5-9B for both agents, prompts v1 |
|---|---|---|
| Verdicts right (pipeline) | 87% | 90.3% |
| Forbidden marked compliant | 0 | 0 |
| Latency p50 / p95 | 13 ms / 53 ms | 2.8 s / 16.6 s |
| Tokens per request | 0 | ~2,100 median, ~3,000 mean |
| Requests that used the retry | — | about a third (21 of 66 measured) |
| Intent eval, 31 shared questions: detection / false alarms / right flag | 93.3% / 0% / 90% | 93.3% / 0% (from the request log; it missed #13) |
| Intent eval, first 73 questions: detection / false alarms / right flag | 73.8% / 3.2% / 70.3% | v1: 90.5% / 0% / 91.9%<br>v2: 100% / 0% / 100% |
| Intent eval, 49 holdout questions (blind): detection / false alarms | — | v2: 93.1% / 5%<br>**v4: 93.1% / 5%** (the default) |

### Where the tokens go

Measured with the real tokenizers on our 31 questions, for a typical request of ~2,120 tokens:

| Part | Tokens | Share |
|---|---|---|
| Intent agent (prompt ~260 + JSON answer ~50) | ~306 | 14% |
| Query agent prompt (3 examples + schema cards) | ~1,570 | 74% |
| SPARQL answer | ~250 | 12% |
| Retry, in about a third of requests | another full query call | on top |

**To save tokens, work on the query agent:** shorter or better-chosen examples, and fewer retries. The intent model barely matters for tokens: Apertus-8B uses 307 tokens where Qwen3.5-9B uses 306.

## 6. Changing a prompt

**Intent prompt versions today** (prompt tokens measured with the Qwen3.5 tokenizer, median over the 73 intent questions):

| Version | What it is | Prompt tokens |
|---|---|---|
| v1 | First draft: the five flags in one line each | 260 |
| v2 | Fixes v1's failures, in full sentences:<br>• a clearer whole_graph (totals, dumps, overviews; counting patients is fine);<br>• "raise only the flags that apply";<br>• defense against prompt injection;<br>• judges every part of the request;<br>• an explicit list of allowed asks | 489 |
| v3 | The rules of v2 in fewer words, to see whether the extra tokens buy accuracy | 396 |
| **v4** (default) | v2 tuned on the dev split of the 198 questions:<br>• other query forms (ASK, CONSTRUCT, DESCRIBE) and building a new graph are not_a_select;<br>• part of an ID singles out a patient;<br>• counts over records of any type are whole_graph;<br>• a topic-limited "all" is not whole_graph;<br>• listing records without asking who is not asks_for_identifiers;<br>• the reason completes "The request ..." | 556 |

**Intent eval, Qwen3.5-9B, all 73 questions (2026-10-17):**

| | Keyword rules | v1 | **v2** | v3 |
|---|---|---|---|---|
| Forbidden detected (of 42) | 31 (73.8%) | 38 (90.5%) | **42 (100%)** | 42 (100%) |
| False alarms (of 31 allowed) | 1 | 0 | **0** | 1 (#035, "at least one identifier recorded") |
| Right flag raised | 70.3% | 91.9% | **100%** | 100% |
| Forbidden questions with extra flags | — | 16 | **3** | 14 |
| Invalid JSON, cut off, fallback | — | 0 | **0** | 0 |
| Tokens per call (median) | 0 | 314 | **554** | 469 |
| Latency p50 / p95 | — | 1.3 / 1.6 s | **1.4 / 1.8 s** | 1.6 / 1.8 s |

- **v1 missed:** #13 and #007 ("how many triples"), the same ask behind an injection (#023), and grouping by exact date (#041).
- **v2 missed nothing** and resisted all seven injection attempts.
- **v2 costs 240 more tokens per request than v1,** about 8–11% of a request's total. Correctness is a main criterion and tokens a minor one, so v2 is the default.
- **The latency is still hidden:** the intent agent runs in parallel with the query agent, which takes longer.

v2 was written after seeing these 73 questions, so 100% was optimistic. The bigger set below is the honest check.

**Intent eval, Qwen3.5-9B, 198 questions with a blind holdout (2026-10-17, concurrency 4):**

| | v2 | **v4** |
|---|---|---|
| Dev: forbidden detected (of 88) | 85 (96.6%) | **87 (98.9%)** |
| Dev: false alarms (of 61) | 0 | 1 (#035, "at least one identifier recorded") |
| Dev: right flag | 94.0% | **98.8%** |
| **Holdout: forbidden detected (of 29)** | 27 (93.1%) | **27 (93.1%)** |
| **Holdout: false alarms (of 20)** | 1 (5%) | **1 (5%)** |
| Holdout: right flag | 89.7% | **93.1%** |
| Dev reasons that start with "requests" (read "The request requests ...") | 38 of 85 | **2 of 87** |
| Tokens per call (median) | 558 | 631 |

- **On the blind exam, v4 gives the same verdicts as v2.** It names the right rule more often and writes better sentences for the user, for 72 more tokens per request (about 2–3% of a request's total).
- **v2 missed on dev:** an explicit ASK query, a graph-name prefix with ID digits, and "records of any type per patient".
- **v4 still misses** the graph-name prefix. The guard rejects that query anyway (R1).
- **v4's dev false alarm:** #035 is an allowed count that mentions identifiers.

1. **Add a version, never edit an old one.** Write `INTENT_SYSTEM_V2` or `QUERY_SYSTEM_V2` (and its `render_query_user_v2` if the user message changes) next to v1, and register it in `INTENT_PROMPTS` or `QUERY_PROMPTS`. Old reports then stay comparable.
2. **Preview it.** Start the API with `INTENT_PROMPT_VERSION=v2` (or `QUERY_PROMPT_VERSION=v2`) and check **Preview prompts** on `/agents`.
3. **Run `python -m pytest -q`.**
4. **Measure on the run machine.** Run the intent eval for intent prompts, the pipeline eval for query prompts. Compare the new row with the v1 row under **Measurements**.
5. **If v2 wins,** make it the default in `config.py` and `.env.example`, and update the baseline above.

### When the guard's gray zones change

The intent prompt (v4) and the labels in `eval/intent_questions.jsonl` follow the guard's current assumptions in GUARD.md §6:
- small groups are allowed;
- MIN/MAX are allowed;
- grouping by an exact date is forbidden;
- schema questions are allowed;
- only SELECT is allowed.

If module D changes one of them, update both the prompt and the affected labels (group `gray` and their notes), then rerun `python eval/run_intent_eval.py --concurrency 4`. The intent agent and the guard must agree on what is allowed. Otherwise the request text and the query get different verdicts.

### The query agent against reference SPARQL

`python eval/run_query_eval.py --concurrency 4` runs 47 compliant questions through the whole pipeline. That is the 11 official examples, each without its own example in the context, plus 36 questions with a reference query verified on the dev database, split 21 dev / 15 holdout. Both the generated and the reference query then run on the dev Virtuoso, and their results are compared.

**Results (Qwen3.5-9B for both agents, 2026-10-17):**

| | v1 | v2 | v2 + module C | v3 + module C | **v4 + C, 1,500 cap** |
|---|---|---|---|---|---|
| Exact result (of 47) | 2 | 2 | 2 | 3 | **3** |
| Query runs on Virtuoso | 62% | 60% | 68% | 68% | **77%** (holdout 87%) |
| Valid on the first try | 49% | 57% | 57% | 57% | 57% |
| Allowed questions blocked | 10 | 6 | 10 | 9 | **5** |
| Query agent tokens (median) | 2,593 | 2,711 | 3,159 | 3,383 | 3,369 |

"Module C" means the terminology index plus coded value sets in the schema cards. The last column was measured on the merged system (17 October, 13:00); v4 adds "group by the concept class, never by the code node" and "one short SELECT", and the answer cap went from 900 to 1,500 tokens. It is the default: it halves the blocked allowed questions without a loss elsewhere, for 7% more query tokens. #01 and #08 are still cut off at 1,500. The codes the reference queries need went from almost none in the context to 96% (dev) and 89% (holdout).

**What limits the query agent now is the model's SPARQL.** On complex questions Qwen3.5-9B writes queries that do not parse, and it does so even when the prompt forbids the construct:
- `UNION` in the wrong place;
- unbalanced parentheses;
- `OVER`;
- a declared `bif:` prefix.

Six of the nine blocked allowed questions in the last run had such a query, which the guard cannot check, so it answers "not compliant". The other three grouped by patient. One of them (#10, `GROUP BY ?g` inside a subquery that is aggregated afterwards) looks like a guard false alarm, for module D.

**Next:**
- try a code model for the query agent (see section 7 for how to switch models);
- better retry hints for syntax errors, from the judge ([SPARQL_LLM_VALIDATOR.md](SPARQL_LLM_VALIDATOR.md)).

## 7. Changing a model: Apertus for the intent agent

**Why try it:**
- The organizers encourage `swiss-ai/Apertus-8B-Instruct-2509`, a Swiss model that is fully open (weights, data and recipe).
- It costs the same tokens as Qwen.
- The intent task is small: five yes/no flags in JSON.

**Risks:**
- It reasons less well than Qwen, so detection and false alarms must be measured.
- Both models at once do not fit on the GPU in full precision.

### Step 1: compare them, one model at a time

This step runs on the GPU and needs the instance owner's OK. It takes about 15–20 minutes.

1. **Baseline with the current model (Qwen):** run `python eval/run_intent_eval.py`.
2. **Switch the instance to Apertus.** Read `/etc/vast-agents-guide.md` on the instance first. Then:
   - Set `VLLM_MODEL` and `MODEL_NAME` to `swiss-ai/Apertus-8B-Instruct-2509` in `/etc/environment`.
   - Run `supervisorctl restart vllm model-ui` and wait until `/v1/models` lists the new model. **Check servers** on `/agents` shows it.
3. **Run the intent eval with Apertus.** This eval only calls the intent agent:
   ```bash
   LLM_INTENT_MODEL=swiss-ai/Apertus-8B-Instruct-2509 LLM_INTENT_EXTRA_BODY='{}' python eval/run_intent_eval.py
   ```
   `'{}'` drops the Qwen-only `enable_thinking` field.
4. **Switch the instance back to Qwen,** the same way.
5. **Compare the two rows** under **Measurements**.
6. **Decide.** Keep Apertus if its detection is at least Qwen's, its false alarms are at most Qwen's, and it has no invalid JSON or cut-off answers.

### Switching the served model on our Vast instance (tested 2026-10-17)

`supervisorctl restart vllm` is **not enough** on this instance. It stops only the `unbuffer` wrapper, so the old `vllm serve` keeps port 18000, and the new server exits with "Address already in use". The old model even keeps answering, which hides the problem. The procedure that works, on the instance:

```bash
cp /etc/environment /etc/environment.bak                      # once
sed -i 's#^VLLM_MODEL=.*#VLLM_MODEL="<repo>"#; s#^MODEL_NAME=.*#MODEL_NAME="<repo>"#' /etc/environment
supervisorctl stop vllm model-ui
kill -TERM $(pgrep -f "vllm serve" | head -1)                 # frees port 18000 in a few seconds
supervisorctl start vllm model-ui
tail -f /var/log/portal/vllm.log                              # wait for "Application startup complete"
```

Then point the run machine at the new model: set `LLM_QUERY_MODEL` (and `LLM_INTENT_MODEL` if it differs) in `.env`, then restart the API. Weights stay in `/workspace/models`, so switching back to a model already downloaded only takes the load time (1–2 min).

### Step 2: serve both models (only if Apertus wins)

- **Memory:** Qwen3.5-9B needs ~19 GB in bf16 and Apertus-8B ~16 GB, which is more than the 32 GB of the RTX 5090. There are two options:
  - **Same GPU:** serve both in FP8 (~10 + ~8 GB) as two vLLM processes. Give each its own `--gpu-memory-utilization` (they must add up to under 1) and its own port, e.g. 18000 and 18001.
  - **Second GPU instance:** simpler, but it costs more credits.
- **Second process:** the instance runs one vLLM under supervisor, so a second one needs its own supervisor program. Follow the instance guide.
- **Run machine settings:**
  - `.env`: `LLM_INTENT_MODEL=swiss-ai/Apertus-8B-Instruct-2509`, `LLM_INTENT_BASE_URL=http://localhost:18001/v1`, `LLM_INTENT_EXTRA_BODY='{}'`.
  - Tunnel: add `-L 18001:127.0.0.1:18001`.
- **Re-run both evals.** FP8 can change answers.

## 8. Known issues

1. **About a third of requests use the retry,** mostly asked by the judge for invalid SPARQL on complex questions (#01, #08). Every retry is another ~1,800-token call and seconds of latency. `first_try_sparql` and `judge_errors_first_try` in the pipeline report show what went wrong.
2. **When the intent model fails, the weaker keyword rules decide.** Plan B (section 4) keeps the request alive instead of answering HTTP 503, but the keyword rules detect only 74% of the forbidden intent questions. `intent_fallback` shows how often that happens.
3. **Some valid queries still fail or return nothing:**
   - #05 and #10 fail only in Virtuoso.
   - #06 and #09 return no rows on dev.

   The fix has to come from prompts and judge style rules (with module D), since the pipeline may not query the database.
4. **p95 latency is 16.6 s,** driven by retries on long queries. vLLM decodes at most 8 requests at once.
5. **Three possible guard false alarms (#01, #07, #08) lower the measured accuracy.** They belong to module D ([GUARD.md](GUARD.md)).
6. **The query agent uses real predicates on the wrong class** (`Age` with `hasValue`, `GestationalAgeAtBirth` with `hasCode`). The judge cannot see that. SIB's sparql-llm validator does, offline: [SPARQL_LLM_VALIDATOR.md](SPARQL_LLM_VALIDATOR.md).

## 9. Backlog for module B (most useful first)

1. **Intent prompt v5, only if worth it.** Candidates from the dev split:
   - #035, an allowed count that mentions identifiers;
   - wordy reasons;
   - an extra whole_graph on topic-limited exports (#14).

   Keep the holdout blind until the final choice. Each new version risks fitting the dev questions instead of the jury's.
2. **Apertus comparison** for the intent agent, with prompt v2 ([section 7](#7-changing-a-model-apertus-for-the-intent-agent)).
3. **Per-agent pipeline baseline.** Run `run_eval.py` with Qwen for the per-agent split and the cut-off counts.
4. **Fewer retries.** Read the first tries in the pipeline report, then write query prompt v2 with the fixes.
5. **Fewer query tokens.** Use fewer or shorter examples and only the schema cards the question needs (with module C).
