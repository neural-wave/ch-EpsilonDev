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
                                         └── one retry, with feedback from ──────┘
                                             the judge or the guard
```

- **The intent agent** reads only the request text. It answers in JSON with five flags (`asks_for_identifiers`, `single_patient`, `individual_rows`, `whole_graph`, `not_a_select`), a reason and a suggested rephrase. It runs in parallel with the context and the query agent, so its time is hidden: the total time is about the query agent's time.
- **The query agent** gets the request and the context (3 similar examples, schema cards, code candidates) and writes one SPARQL query. It translates literally, forbidden parts included, so the guard can judge them.
- **The judge and the guard are code, not models** (module D, see [GUARD.md](GUARD.md)). The judge checks syntax, prefixes and terms absent from the data. The guard applies the five rules to the query and combines them with the intent flags.

**Retry rules.** Each request has one retry, shared:
- **The judge** asks for it when the query is invalid. It happens before the intent result is known.
- **The guard** asks for it only when the request is not flagged and the query breaks a rule.
- **A flagged request** never gets a guard retry.

The retry is decided by code, not by the agents. The judge or the guard finds the problem, and the orchestrator calls the query agent again. The query agent only sees the feedback added to its prompt. Module B's part of the retry is how well the model corrects itself and how many tokens that costs.

**Stand-ins.** With `NL2SPARQL_BACKEND=stub`, no model is used:
- `HeuristicIntentAgent` applies keyword rules.
- `ExampleQueryAgent` copies the SPARQL of the most similar example.

They only let the pipeline run end to end without a GPU.

## 2. Code map

| File | Role |
|---|---|
| `agents/base.py` | The two interfaces (shared with the team): `classify(text) -> (flags, tokens)` and `generate(text, context, feedback) -> (sparql, tokens)` |
| `agents/intent_llm.py` | `LLMIntentAgent`. `classify_detailed()` returns an `IntentReply`: flags, tokens, raw answer, `valid_json`, `finish_reason` |
| `agents/query_llm.py` | `LLMQueryAgent`. `generate_detailed()` returns a `QueryReply`: SPARQL, tokens, raw answer, `finish_reason`. `extract_sparql()` takes the query out of a code fence |
| `agents/prompts.py` | The prompts by version (`INTENT_PROMPTS`, `QUERY_PROMPTS`) |
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
| Answer cap | `LLM_INTENT_MAX_TOKENS` (200) | `LLM_QUERY_MAX_TOKENS` (900) |
| Prompt version | `INTENT_PROMPT_VERSION` (v1) | `QUERY_PROMPT_VERSION` (v1) |
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
| `retry_reason` | `judge` or `guard`: who asked for the retry |
| `first_try_sparql` | The query the retry replaced |
| `judge_errors_first_try`, `guard_violations_first_try` | Why the first try was rejected |
| `judge_errors_final` | Still invalid after the retry |
| `cut_off` | Steps whose answer hit the answer cap: `intent`, `query`, `query_retry` |
| `intent_invalid_json` | The intent answer was not the expected JSON. All flags read as false |
| `intent_error` | The intent call failed. All flags read as false |

When the query agent's answer was cut off and the judge rejects it, the retry feedback starts with *"Your previous answer was cut off at the length limit. Write a shorter query and no explanation."*

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
python eval/run_intent_eval.py        # the intent agent alone: 31 short calls
python eval/run_eval.py               # the whole pipeline, per-agent numbers in "per_agent"
python eval/run_eval.py --execute     # also runs compliant queries on the dev Virtuoso
```

**Questions:** 31 in total, made of the 16 official examples and the 15 team questions in `eval/questions.jsonl`.

**Reports:** `eval/reports/` is git-ignored. Every report records the models, prompt versions and answer caps it ran with, so reports stay comparable.

**Intent eval metrics:**

| Metric | Meaning | Better |
|---|---|---|
| Detection | Forbidden questions that raised a flag | Higher |
| False alarms | Allowed questions that raised a flag | Lower |
| Right flag | Questions naming a rule that raised that rule's flag | Higher |
| Invalid JSON, cut off | Unusable answers | 0 |
| Tokens, latency | Cost of one call | Lower |

### Baseline (2026-10-17)

| | Stub | Qwen3.5-9B for both agents, prompts v1 |
|---|---|---|
| Verdicts right (pipeline) | 87% | 90.3% |
| Forbidden marked compliant | 0 | 0 |
| Latency p50 / p95 | 13 ms / 53 ms | 2.8 s / 16.6 s |
| Tokens per request | 0 | ~2,100 median, ~3,000 mean |
| Requests that used the retry | — | about a third (21 of 66 measured) |
| Intent eval: detection / false alarms / right flag | 93.3% / 0% / 90% | not run yet (backlog item 1) |

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

1. **Add a version, never edit an old one.** Write `INTENT_SYSTEM_V2` or `QUERY_SYSTEM_V2` (and its `render_query_user_v2` if the user message changes) next to v1, and register it in `INTENT_PROMPTS` or `QUERY_PROMPTS`. Old reports then stay comparable.
2. **Preview it.** Start the API with `INTENT_PROMPT_VERSION=v2` (or `QUERY_PROMPT_VERSION=v2`) and check **Preview prompts** on `/agents`.
3. **Run `python -m pytest -q`.**
4. **Measure on the run machine.** Run the intent eval for intent prompts, the pipeline eval for query prompts. Compare the new row with the v1 row under **Measurements**.
5. **If v2 wins,** make it the default in `config.py` and `.env.example`, and update the baseline above.

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
2. **The judge retry also runs for requests the intent agent will flag,** because the intent result arrives in parallel. That spends query-agent tokens on a forbidden request, but it also gives the jury a valid query to judge. When to retry is decided by the judge and the orchestrator (modules D and A), not by the agents.
3. **An unusable intent answer reads as "no flags".** This covers invalid JSON, a cut-off answer or a failed call. The request then relies on the guard's check of the query alone. It is visible now (`intent_invalid_json`, `cut_off`, `intent_error`) but not handled.
4. **Some valid queries still fail or return nothing:**
   - #05 and #10 fail only in Virtuoso.
   - #06 and #09 return no rows on dev.

   The fix has to come from prompts and judge style rules (with module D), since the pipeline may not query the database.
5. **p95 latency is 16.6 s,** driven by retries on long queries. vLLM decodes at most 8 requests at once.
6. **Three possible guard false alarms (#01, #07, #08) lower the measured accuracy.** They belong to module D ([GUARD.md](GUARD.md)).

## 9. Backlog for module B (most useful first)

1. **Per-agent baseline on the GPU.** Run `run_intent_eval.py` and `run_eval.py` with Qwen. This gives the first intent-alone numbers, the per-agent split and the cut-off counts.
2. **Apertus comparison** for the intent agent ([section 7](#7-changing-a-model-apertus-for-the-intent-agent)).
3. **Fewer retries.** Read the first tries in the pipeline report, then write query prompt v2 with the fixes.
4. **Fewer query tokens.** Use fewer or shorter examples and only the schema cards the question needs (with module C).
5. **Intent prompt v2.** Cover prompt injection and the gray zones, with new questions in `eval/questions.jsonl` (with module E).
6. **Unusable intent answers.** Decide between one retry and failing closed (issue 3).
