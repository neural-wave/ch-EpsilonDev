# NL-to-SPARQL Gateway

**SIB / SPHN challenge · Neural Wave hackathon 2026 · team master document**

A researcher asks a question in English. We translate it into a SPARQL query over SPHN clinical data and decide whether that query respects the privacy rules. Everything sits behind one endpoint, `POST /nl-to-sparql`, uses only open-source models, and never touches a database at request time.

This README is the single source of truth for the team. Read it once end to end before you start, then use the contents to jump around. If you work with an AI coding assistant, it also reads [AGENTS.md](AGENTS.md).

*Last updated: Saturday 17 October 2026, 05:15, after merging modules A, B, C and D. Deadline: **Sunday 18 October, 11:00**.*

## Contents

1. [The challenge](#1-the-challenge)
2. [What the jury told us](#2-what-the-jury-told-us)
3. [Status today](#3-status-today)
4. [Architecture and design decisions](#4-architecture-and-design-decisions)
5. [Repository map and module owners](#5-repository-map-and-module-owners)
6. [Getting started on your computer](#6-getting-started-on-your-computer)
7. [Configuration reference](#7-configuration-reference)
8. [The LLM on the GPU](#8-the-llm-on-the-gpu)
9. [The run machine](#9-the-run-machine)
10. [The knowledge base and the data files](#10-the-knowledge-base-and-the-data-files)
11. [Evaluation and baseline numbers](#11-evaluation-and-baseline-numbers)
12. [Things we learned the hard way](#12-things-we-learned-the-hard-way)
13. [Next steps per module](#13-next-steps-per-module)
14. [Questions for the organizers](#14-questions-for-the-organizers)
15. [Team rules](#15-team-rules)
16. [Submission checklist](#16-submission-checklist)
17. [Glossary](#17-glossary)
18. [Team](#18-team)
19. [Add-ons from the team (17 October evening)](#19-add-ons-from-the-team-17-october-evening)

---

## 1. The challenge

**Context.** In the Swiss Personalized Health Network (SPHN), every hospital keeps its patient data as an RDF knowledge graph in its own triplestore. Researchers rarely write SPARQL, and SPARQL is powerful enough to re-identify patients. Only pre-approved query shapes should ever reach a hospital database.

**What we build.** Two components behind one API:

1. a **translator**: natural-language request → SPARQL query;
2. a **compliance checker**: does that query respect the privacy rules?

**API contract** (from the organizers' `API_specification.md`, fixed):

```
POST /nl-to-sparql        {"text": "How many patients have ... ?"}
200 → {"sparql_query": "...", "tokens_consumed": 1287, "is_compliant": true}
200 → {"sparql_query": "...", "tokens_consumed": 1540, "is_compliant": false,
       "compliance_reason": "..."}                  # a forbidden request is still a 200
4xx/5xx → {"error": "..."}
```

`tokens_consumed` is the total over every model call made for the request.

**Privacy rules** (from `Query_Compliance.md`):

| Rule | Forbidden | Official example of a violation |
|---|---|---|
| R1 | Queries about a single, identifiable patient | #11 "How many lab tests has patient 0042 had?" |
| R2 | Results that are not aggregated across all patients | #12 "Show me each cholera case with date, age and insurance" |
| R3 | Matching the whole graph without restriction | #13 "How much data do we have in total?" |
| R4 | Anything but `SELECT` | #14 "Give me the cancer diagnoses as a graph I can take away" (CONSTRUCT) |
| R5 | Exposing identifiers (patient, lab, diagnosis IDs…) | #15 "How many patients have cholera, and which ones are they?" |

**How we are scored.** The jury sends a hidden list of requests, each labeled compliant or non-compliant, to our API.

| Criterion | What is measured |
|---|---|
| 1. Efficiency (main) | Total time per request: translation + compliance + retries |
| 2. Compliance correctness (main) | Our `is_compliant` against their label. False positives and false negatives both cost points |
| 3. Query performance (main) | Execution time of our query on their machine, average of 10 isolated runs |
| Minor | Tokens per request, smaller models preferred, RAM usage |

**Hard constraints.**

- Every tool and **every model** must have an open-source license. The organizers encourage Apertus. No Claude, no GPT inside the system.
- Installs with `pip install -r requirements.txt` and works out of the box: the jury will not debug.
- **The repo becomes public after the event.** Restricted terminologies (SNOMED CT, ICD-10-GM, CHOP, LOINC, UCUM), anything derived from them, patient data and secrets must never be committed.

**Source material** (not in this repo, ask Santiago):

- The challenge brief (PDF).
- The organizers' release `hackathon-virtuoso-release.zip`: Virtuoso scripts, ontologies, SHACL shapes, the 15 example queries, the API spec and the rules.
- The 100,000-patient dataset `gen_mock_data-1_1000001-1100000.tar.zst`.

## 2. What the jury told us

Feedback on our design, 16 October:

- **The system must be database-agnostic.** Nothing behind the endpoint may query Virtuoso or any other database. The database is only for us to check performance and results.
- **Executing compliant queries is not required.** Our demo does it only as an optional dev check.
- **A real user asking something forbidden gets only the reason, never the query.** For the hackathon, the API still returns the literal query so the jury can judge it. `RETURN_NONCOMPLIANT_QUERY=false` switches to the real behavior.
- They liked the overall plan.

## 3. Status today

| Area | State |
|---|---|
| Modules A–D | **Merged on `main`** (17 October): integration with timeouts and safe retries (David), agents and context (Santiago), judge and context hardening (Adrika), compliance guard (Gayathri). 244 tests pass locally and in pull-request CI |
| LLM | **Qwen3.6-27B** (4-bit AWQ build `cyankiwi/Qwen3.6-27B-AWQ-INT4`, Apache-2.0) served by vLLM on a Vast.ai RTX 5090, for both agents, since 17 October 21:00. It replaced Qwen3.5-9B after a measured test; Coder-14B, Coder-30B and Qwen3.8-27B were tested and not adopted ([section 8](#8-the-llm-on-the-gpu), and the full history with the reasons in [docs/MODEL_HISTORY.md](docs/MODEL_HISTORY.md)) |
| Verdicts, 31 shared questions | 90.3% correct, 0 forbidden requests marked compliant, 3.1 s / 19.2 s latency (p50 / p95), measured with Qwen3.5-9B on 17 October morning |
| Verdicts, 198 labeled questions | **99.3%** on the dev split and **100% on the blind holdout** (0 forbidden requests marked compliant, 0 allowed requests blocked), p50 5.3 s, p95 17.9 s ([section 11](#11-evaluation-and-baseline-numbers)) |
| Query quality | **48 of 87** generated queries return the reference's numbers (**19 of 31** on the blind holdout, 1 of 11 official examples), 92% run on Virtuoso, 83% pass the judge on the first try |
| Context | Terminology index (38,765 codes) and coded value sets (55 properties) built on the run machine; 96% of the codes reference queries need reach the query agent |
| Knowledge base | Two local Virtuoso databases: dev (2,000 patients) and perf (100,000 patients) |
| Demo page and agents console | `http://localhost:8080` and `/agents` on the run machine |
| GitHub | `main` on EpsilonDev26/ep-rep; the official `NW_26_<Company>_<Team>` repo is pending |
| Organizer answers | Several questions still open (see [section 14](#14-questions-for-the-organizers)) |

## 4. Architecture and design decisions

```
POST /nl-to-sparql {"text"}                                           ┌──────────────────────┐
  │                                                                   │ Knowledge base       │
  ▼                                                                   │ Virtuoso, RDF graph  │
Input check (code) ── empty / too long ──► 400 {"error"}              │ (dev and perf only)  │
  │                                                                   └──────────┬───────────┘
  ├────────────────────────────────┐   in parallel                               │ built once, offline:
  ▼                                ▼                                             │ data/*.json
Intent agent (LLM)          Context (code) ◄─────────────────────────────────────┘
5 flags + reason            similar examples · schema cards · code candidates
+ suggested rephrase               │
  │                                ▼
  │                         Query agent (LLM) ◄─────────────┐
  │                                │ SPARQL                 │ 1 retry, shared
  │                                ▼                        │
  │                         Judge (code) ── syntax/terms ───┤
  ▼                                ▼                        │
Compliance guard (code): 5 rules on the final query ────────┘ (allowed request, broken rule)
  + intent flags
  │
  ▼
200 {sparql_query, tokens_consumed, is_compliant, compliance_reason?}
  ┆ outside the API, optional (demo / eval only)
  ├─ compliant     → run on Virtuoso, show the aggregate
  └─ non-compliant → nothing runs, show the reason and "Ask this instead"
```

A visual version with module letters and a "who works where" panel lives on the team diagram page: <https://claude.ai/artifact/9noDnsmYxSdoF37BMZAcxd>. It is private, so ask Santiago to share it with you. The full design spec is in [docs/specs/2026-10-16-nl-to-sparql-design.md](docs/specs/2026-10-16-nl-to-sparql-design.md).

### Decisions and why

1. **Literal translation.** The query agent never drops or softens part of the request. The jury labels requests, so if the model "cleans up" a forbidden request into a harmless count, we would say compliant and lose the point.
2. **The verdict looks at both the request and the query.**
   - The **intent agent** reads the text and flags forbidden asks.
   - The **guard** checks the five rules on the query itself.
   - Each sees what the other misses: a query cleaned up by the model, or a query that leaks rows on an allowed request.
3. **Code wherever possible.** Input check, context, judge and guard are deterministic code: milliseconds, zero tokens, same answer every time. The parse plus guard takes about 15 ms per query.
4. **One LLM call on the critical path.** The intent agent runs in parallel with generation, never as a gate before it.
5. **One retry each for the judge and the guard, plus one after a retry spent on syntax.** The judge (syntax, unknown terms and the checks in `judge_checks/`) may ask the model again once, and the guard (an allowed request whose query broke a rule) once more. When the judge's retry was bought by syntax errors alone and the retry parses but is still not clean, the judge asks once more (`retry_reason` `judge x2`): that retry is the first answer the semantic checks ever saw, and on the 17 October run 13 of 56 dev requests spent the judge's only retry on syntax and none ended right. So the query agent runs at most four times per request, always inside `REQUEST_TIMEOUT_SECONDS` (the extra call is skipped when less time remains than the longest query-agent call so far). A retry that is still not clean is a fresh query, not a repaired one: after each retry, the judge's or the guard's, the pipeline keeps the less bad attempt by the judge's severity (`nl2sparql.judge.severity`), so a guard retry that comes back unparseable or cut off never replaces an attempt that parsed. A cut-off answer tells the retry to be shorter, whichever checker asked for it.
6. **A forbidden request is never retried.** A retry could turn it into a clean query, which we would then wrongly mark compliant. An R1 violation on the query is treated the same way: the model cannot invent a graph name, a record IRI or identifier digits, so the request named them (`REQUEST_LEVEL_RULES` in `guard/decision.py`).
7. **A query that breaks a rule is never marked compliant**, even if that costs us a point.

### How the guard decides

| Intent agent (request) | Query check | What happened | Verdict |
|---|---|---|---|
| OK | OK | Normal case | compliant |
| flagged | broken | Both agree the request is forbidden | non-compliant, no retry |
| flagged | OK | The query agent cleaned up a forbidden request | non-compliant, no retry |
| OK | broken (R1) | The query names a patient graph, a record or an identifier: that came from the request, not from the model | non-compliant, no retry |
| OK | broken (R2–R5) | The query agent made a mistake on an allowed request | 1 retry naming the broken rule and, for grouping mistakes, what to group by instead; still broken means non-compliant |

`compliance_reason` is built from three parts:

1. A fixed template per broken rule, naming the offending part of the query.
2. The intent agent's reason.
3. A rephrase suggestion: the intent agent's `suggested_rephrase`, or a fixed hint per rule.

### What the API returns

| Situation | HTTP | Body |
|---|---|---|
| Compliant request | 200 | `sparql_query`, `tokens_consumed`, `is_compliant: true` |
| Forbidden request | 200 | literal `sparql_query` (for the jury only, never executed), `tokens_consumed`, `is_compliant: false`, `compliance_reason` |
| Empty or oversized text | 400 | `{"error": "'text' must not be empty."}` |
| Pipeline failure (e.g. model unreachable) | 503 | `{"error": "..."}` |

## 5. Repository map and module owners

```
nl2sparql/
├── README.md                     this document
├── AGENTS.md, CLAUDE.md          context for AI coding assistants
├── requirements.txt, pyproject.toml, .env.example
├── src/nl2sparql/
│   ├── api.py, __main__.py       FastAPI app: /nl-to-sparql, /health, demo routes        (A)
│   ├── orchestrator.py           the pipeline: parallel calls, shared retry, timings     (A)
│   ├── factory.py, config.py     wiring from settings                                     (A)
│   ├── input_check.py            normalize text, reject empty/oversized                   (A)
│   ├── load_test.py              concurrent HTTP check of the judged endpoint             (A)
│   ├── models.py                 shared types                                         (shared)
│   ├── agents/                   intent + query agents, LLM and stand-in versions        (B)
│   │   ├── base.py               agent interfaces                                     (shared)
│   │   ├── prompts.py            prompts by version (v1, v2, ...)                         (B)
│   │   └── console.py, .html     /agents: agents console and measurements                 (B)
│   ├── llm.py                    OpenAI-compatible client                                 (B)
│   ├── context/                  examples (BM25), schema cards, terminology index        (C)
│   ├── guard/                    the five rules + verdict logic                           (D)
│   ├── judge.py                  repairs, syntax, terms absent from the data, then the    (D)
│   │                             checks below
│   ├── judge_checks/             one module per family: syntax (explains and repairs what
│   │                             rdflib rejects), scope, semantics, schema, the question
│   │                             checks (coverage, record count) and the rewrites of
│   │                             shapes with one correct form (scope, aggregation)        (D)
│   ├── sparql/                   Virtuoso-aware parsing and prefix fixes, shared by guard and judge (D)
│   ├── virtuoso.py               small client (scripts, eval, demo only)                  (C)
│   └── demo/index.html           demo page                                                (E)
├── data/                         committed data files (no restricted content)             (C)
├── scripts/                      import examples, profile data, terminology index,
│                                 value sets, profile comparison, retrieval benchmark, runbox (C)
├── infra/virtuoso/               how to run the two Virtuoso databases                    (C)
├── eval/                         labeled questions and the evals: pipeline, intent, query (E)
├── tests/                        unit tests, one file per module
└── docs/                         guides per module, design spec, implementation plan
```

| | Module | Owns | Tests | Owner |
|---|---|---|---|---|
| **A** | Integration | API, orchestrator, factory, config, input check, load test, deployment | `tests/test_pipeline_and_api.py`, `tests/test_input_check.py` | David |
| **B** | Agents and prompts | `agents/`, `llm.py`, model choice | `tests/test_agents_and_judge.py` | Santiago |
| **C** | Data and context | `context/`, `scripts/`, `data/`, `infra/`, `virtuoso.py` | `tests/test_context.py` | Santiago (Adrika hardened the loaders and retrieval) |
| **D** | Guard and judge | `guard/`, `judge.py`, `sparql/` | `tests/test_guard.py`, `tests/test_sparql.py` | Adrika (judge), Gayathri (compliance guard) |
| **E** | Eval and demo | `eval/`, `demo/`, report and video numbers | `python eval/run_eval.py` | |

**Shared interfaces**: [models.py](src/nl2sparql/models.py) and [agents/base.py](src/nl2sparql/agents/base.py). Change them only after agreeing with the team.

**Guides per topic:**
- [docs/DATA.md](docs/DATA.md): how the patient graphs look, with examples. For everyone.
- [docs/data-and-context/README.md](docs/data-and-context/README.md): context module checklist, split between local tests and real-graph validation.
- [docs/GUARD.md](docs/GUARD.md): how the compliance guard works, its tools and its backlog. For module D.
- [docs/guard-and-judge/README.md](docs/guard-and-judge/README.md): what was verified locally in the guard and the judge, and what needs the run machine.
- [docs/SPARQL_LLM_VALIDATOR.md](docs/SPARQL_LLM_VALIDATOR.md): SIB's sparql-llm validator, evaluated offline, and the Virtuoso-only errors the judge could catch. For module D.
- [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md): the two LLM agents, their configuration, how to measure them, how to change a prompt or a model, and their backlog. For module B.
- [docs/MODEL_HISTORY.md](docs/MODEL_HISTORY.md): every change to the model and its configuration, why it was made, what was measured and what was decided. For everyone, and for the report.
- [docs/FRONTEND.md](docs/FRONTEND.md): the endpoint the user interface calls, its fields and the rules the page must keep. For module E.
- [docs/README.md](docs/README.md): the index of every guide, including the add-on documents of section 19 (privacy review, results validation, feature handoff, project handbook).
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md): how we measure, the scoreboard of every measured run, where the time and tokens go, and the levers. For everyone who changes anything.

## 6. Getting started on your computer

You need Python 3.11 or newer. You do **not** need Virtuoso or a GPU to work on the code.

```bash
git clone <repo-url> nl2sparql && cd nl2sparql
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m pytest -q                 # all tests should pass
python -m nl2sparql --port 8080     # API + demo with the stand-in agents (no model)
```

In a second terminal, check that the process is serving requests:

```bash
curl -fsS http://localhost:8080/health
curl -s localhost:8080/nl-to-sparql -H 'Content-Type: application/json' \
  -d '{"text": "How many patients have a billed diagnosis of cholera, by sex?"}'
curl -i -s localhost:8080/nl-to-sparql -H 'Content-Type: application/json' \
  -d '{"text": "How many lab tests has patient 0042 had?"}'
```

`/health` should report `{"status":"ok","backend":"stub"}`. The allowed request returns the fixed API fields; the forbidden request still returns HTTP 200 with `is_compliant: false` and `compliance_reason`. The stub check verifies wiring and the HTTP contract, not translation quality.

**Without a model** (`NL2SPARQL_BACKEND=stub`), the intent agent uses keyword rules and the query agent copies the SPARQL of the most similar example. It is not a translator; it only lets the pipeline run end to end. **To use the real LLM**, see [section 8](#8-the-llm-on-the-gpu).

### For the jury: the full system on your machine

The judged API needs a model server; everything else is in this repository. Four steps:

1. **Install.** Python 3.11 or newer, then `python -m pip install -r requirements.txt`.
2. **Configure.** Copy `.env.example` to `.env` and set:
   - `NL2SPARQL_BACKEND=llm`.
   - The model server: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_QUERY_MODEL=cyankiwi/Qwen3.6-27B-AWQ-INT4` and `LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'`. We hand over the endpoint and token of our own server with the submission. To serve the model yourself instead (a GPU with 32 GB or more, vLLM 0.30; the weights take 19.6 GB): `vllm serve cyankiwi/Qwen3.6-27B-AWQ-INT4 --max-model-len 32000 --max-num-seqs 8 --port 8000`, then `LLM_BASE_URL=http://localhost:8000/v1`.
   - `VIRTUOSO_ENDPOINT`: your SPARQL endpoint. Only step 3 reads it; the API never does.
3. **Build the local assets** (about a minute): `python scripts/build_assets.py`. It reads the ontologies in your Virtuoso and writes the terminology index and the value sets to `assets/`. They derive from restricted terminologies, so they are not in the repository. This step is optional: without it the API runs with fewer resolved medical codes.
4. **Run.** `python -m nl2sparql --port 8080`, then `POST http://localhost:8080/nl-to-sparql` with `{"text": "..."}` as in [section 1](#1-the-challenge).

The committed `data/` snapshots describe the mock data as released. If your graph differs, `python scripts/profile_data.py` rebuilds them in about three minutes (`--cards-only` rebuilds the cards alone in one).

**Agents console:** <http://localhost:8080/agents> shows the full configuration of both agents and their prompts. It can:
- preview exactly what each model would receive for a question, without calling any model;
- run one question and show what each agent did (with the LLM backend, this calls the model);
- list every saved eval report, with a scoreboard of the jury's criteria.

**Runs page:** <http://localhost:8080/agents/runs> charts the main metrics over every run and compares two runs question by question (what got fixed, what broke), with a note per run saying what it tested. `python eval/run_all.py --label "what changed"` runs all four evals for the current configuration and writes that note.

See [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md).

## 7. Configuration reference

All settings come from environment variables; `.env.example` lists them. Load them with `set -a && source .env && set +a`.

| Variable | Default | What it does |
|---|---|---|
| `NL2SPARQL_BACKEND` | `stub` | `stub` (no model) or `llm` |
| `LLM_BASE_URL` | `http://localhost:8000/v1` | OpenAI-compatible server. Through the SSH tunnel: `http://localhost:18000/v1` |
| `LLM_API_KEY` | `not-needed` | Bearer token; needed for the public Vast endpoint, not for the tunnel |
| `LLM_QUERY_MODEL` | — | Model name served by vLLM, e.g. `cyankiwi/Qwen3.6-27B-AWQ-INT4` |
| `LLM_INTENT_MODEL` | query model | A separate model for the intent agent |
| `LLM_INTENT_BASE_URL`, `LLM_INTENT_API_KEY` | shared values | Give the intent agent its own model server |
| `LLM_INTENT_EXTRA_BODY` | inherits `LLM_EXTRA_BODY` | Request fields for the intent model only; `'{}'` sends none |
| `LLM_INTENT_MAX_TOKENS`, `LLM_QUERY_MAX_TOKENS` | `200`, `1500` | Answer length cap per agent. Tokens are spent only when the answer needs them |
| `INTENT_PROMPT_VERSION`, `QUERY_PROMPT_VERSION` | `v4`, `v4` | Which prompt version each agent uses (see `src/nl2sparql/agents/prompts.py` and docs/LLM_AGENTS.md §6) |
| `QUERY_REORDER` | `true` | `true` runs `nl2sparql.sparql.order` after the query agent: when the query has exactly one typed code pattern, inside the first `GRAPH ?g` block, and no UNION, MINUS or sub-select, the pass puts that pattern first, rotates the statement that links to it so `sphn:hasCode ?c` is its first triple, and prepends `DEFINE sql:select-option "order"`; every other query is returned untouched (a forced order is only as good as the whole written query: q-015 went from 0.19 s to 9 s when a `;` chain started with the class pattern). On by default since 17 Oct: on 100,000 patients the four run-1 queries it rewrites gave identical rows on dev and q-021 went from 0.44 s to 0.06 s, the others within 0.1 s |
| `CONTEXT_EXAMPLES`, `CONTEXT_CARDS`, `CONTEXT_CODES` | `3`, `8`, `5` | How many similar examples, schema cards and code candidates the query agent receives per request |
| `CONTEXT_DEV_REFERENCES` | `true` | `true` also loads the dev rows of `eval/query_questions.jsonl` (question + reference SPARQL, 45 today) into the example bank, after the official and team examples so an official example wins a tie; holdout rows are never read. Their shape tag is read from the SPARQL. With it on, only the holdout numbers of the eval are meaningful: a dev question no longer finds its own reference (excluded by text) but retrieves its neighbours'. On since 17 October: on the blind holdout, right numbers 7 → 10 of 31 and queries that run 81% → 94%, with fewer tokens and retries. Does nothing where `eval/` is absent |
| `LLM_TIMEOUT_SECONDS` | `30` | Per LLM call |
| `LLM_VERIFY_TLS` | `true` | `false` for servers with a self-signed certificate |
| `LLM_EXTRA_BODY` | empty | JSON merged into every LLM request. For Qwen3.x: `'{"chat_template_kwargs": {"enable_thinking": false}}'` (keep the single quotes) |
| `VIRTUOSO_ENDPOINT` | `http://localhost:8890/sparql` | Dev only: scripts, `eval --execute`, the demo's dev check |
| `MAX_INPUT_CHARS` | `2000` | Longer requests get a 400 |
| `MAX_RETRIES` | `1` | Retries per checker: the judge may ask the model again once (twice when its first retry was spent on syntax errors alone) and the guard once, so the query agent runs at most four times per request |
| `RETRY_WITH_PREVIOUS_QUERY` | `false` | `true` adds the rejected query to the retry feedback. Measured on 17 October: the model then copies the broken query; fewer retries end valid |
| `REQUEST_TIMEOUT_SECONDS` | `75` | Total time limit for one pipeline request, including agent calls and retries. Expiry returns HTTP 503; use a positive number of seconds |
| `RETURN_NONCOMPLIANT_QUERY` | `true` | Hackathon behavior. `false` = a forbidden request returns an empty `sparql_query` |
| `GPU_SSH_HOST`, `GPU_SSH_PORT` | — | Run machine only, for the tunnel (ask Santiago) |
| `API_PORT` | `8080` | Port used by `scripts/runbox.sh` |
| `NL2SPARQL_DATA_DIR`, `NL2SPARQL_ASSETS_DIR`, `NL2SPARQL_LOG_PATH`, `NL2SPARQL_REPORTS_DIR` | `data/`, `assets/`, `logs/requests.jsonl`, `eval/reports/` | Where data, built assets, the request log and the eval reports live |
| `DEMO_SHOW_DETAILS` | `true` | The team console shows the generated query, the specific reason of a block, the suggested rephrase and the retry diagnostics in `/demo/ask`. `false` hides them (section 19) |
| `DEMO_PUBLIC_MODE` | `false` | Public end-user console: only `/`, `/health`, `/demo/*` and `/console/*` answer, nothing is logged, and the details above are hidden. Needs `DEMO_MIN_GROUP_SIZE`. Never on the jury's API |
| `DEMO_MIN_GROUP_SIZE` | `disabled` | Demo only: hide a whole result when a group has fewer patients. Off until the organizers approve a policy |
| `DEMO_SHARE_TTL_SECONDS`, `DEMO_SHARE_DB_PATH` | `604800`, `logs/shared_results.sqlite3` | Lifetime and store of the opt-in shared result links (aggregate tables only, git-ignored) |
| `DEMO_DATASET_NAME`, `DEMO_DATASET_VERSION`, `DEMO_DATASET_DESCRIPTION` | empty | The reproducibility card's dataset identity; empty shows "Not configured" |

## 8. The LLM on the GPU

| | |
|---|---|
| Provider | Vast.ai, on-demand instance rented by Santiago (about $100 of credit) |
| GPU | 1× NVIDIA RTX 5090, 32 GB |
| Server | vLLM 0.30 (Vast "vLLM" template), OpenAI-compatible API |
| Model | `cyankiwi/Qwen3.6-27B-AWQ-INT4`: the 4-bit AWQ build of `Qwen/Qwen3.6-27B` (27.8B parameters, dense, Apache-2.0, not gated). 19.6 GB of weights; the KV cache holds 102,808 tokens. Until 17 October 21:00: `Qwen/Qwen3.5-9B` |
| Server settings | `--max-model-len 32000 --max-num-seqs 8` (at most 8 requests decoded at once) |
| Both agents | Same model, different prompts. Separate it only if the eval says so |

**Turn thinking off.** Qwen3.5 and Qwen3.6 think by default. In our first test it spent its whole 600-token budget reasoning and returned nothing (7 s). With thinking off it answered in 1.2 s with 47 tokens. Always set `LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'`.

**How to reach it.**

- **SSH tunnel** (what the run machine uses): `ssh -N -p <port> -L 18000:127.0.0.1:18000 root@<host>`, then `LLM_BASE_URL=http://localhost:18000/v1` with no token. Santiago adds your SSH public key to the instance first.
- **Public endpoint:** `http://<host>:<external port>/v1` with `LLM_API_KEY=<instance token>`. Ask Santiago for both. Never commit them.

**Changing the model** is done on the instance by its owner. Per the instance's own guide (`/etc/vast-agents-guide.md`, which AI agents must read before acting on the instance):

1. Set both `VLLM_MODEL` and `MODEL_NAME` in `/etc/environment`.
2. Stop the services with `supervisorctl stop vllm model-ui`.
3. Kill the old server with `kill -TERM $(pgrep -f "vllm serve" | head -1)`. A plain restart leaves it holding port 18000, and the new server then fails with "Address already in use".
4. Start them again with `supervisorctl start vllm model-ui`.

The full procedure is in [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md). vLLM listens on `127.0.0.1:18000`; Caddy in front adds token auth. Do not add `--api-key` to vLLM.

**Models compared on 17 October** (query eval: 47 questions with a reference SPARQL; intent eval: 198 questions):

| Model (Apache-2.0) | Exact results | Query runs | Latency p50 / p95 | Intent holdout: detected / false alarms |
|---|---|---|---|---|
| `Qwen/Qwen3.5-9B` (in use) | 2–3 of 47 | 68% | 6.9 / 19.0 s | 93% / 5% |
| `Qwen/Qwen2.5-Coder-14B-Instruct-AWQ` | 2–3 of 47 | 72–74% | 4.5 / 10.9 s | not measured |
| `cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit` | 3 of 47 | 60% | 3.6 / 7.5 s | 93% / 10% |

That morning a bigger model did not improve the SPARQL; it only changed speed.

**Models compared on 17 October evening**, on the final pipeline (judge with deterministic repairs, 3 examples from a bank that includes the dev reference queries). Right numbers = the reference's numbers, possibly with other labels or rounding; the blind holdout has 31 questions:

| Model (Apache-2.0) | Right numbers, holdout | Official examples | Holdout queries that run | Holdout first try | Latency p50 / p95 | Verdicts dev / holdout (198) |
|---|---|---|---|---|---|---|
| `Qwen/Qwen3.5-9B` (until 21:00) | 10 of 31 (two runs) | 1 of 11 | 87–94% | 48–52% | 8.7 / 28.2 s | 95.3% / 93.9% |
| **`cyankiwi/Qwen3.6-27B-AWQ-INT4` (in use)** | **19 of 31 (three runs)** | 1 of 11 | **94%** | **81%** | **7.2 / 21.9 s** | **99.3% / 100%** |
| `cyankiwi/Qwen3.8-27B-AWQ-INT4` | 16 of 31 (one run) | 1 of 11 | 84% | 81% | 9.3 / 20.5 s | not measured |

Qwen3.6-27B nearly doubles the right numbers and is not slower: it passes the judge on the first try far more often, so it needs fewer retries (median tokens per question 5,913 → 3,325). The newer Qwen3.8-27B gave fewer right numbers and more Virtuoso errors. Bigger open models need several GPUs and were not tested. Every model change, with its reason and its reports, is in [docs/MODEL_HISTORY.md](docs/MODEL_HISTORY.md). `swiss-ai/Apertus-8B-Instruct-2509`, the model the organizers encourage, is still a candidate for the intent agent ([docs/LLM_AGENTS.md](docs/LLM_AGENTS.md), section 7).

**Cost.** Vast bills per hour while the instance runs, not per request, so run as many tests as you like. Stopping it halts GPU billing; storage is still billed. After a stop, the GPU may be taken by someone else when you restart.

## 9. The run machine

We run everything heavy on one computer, **Santiago's laptop**. It has the two Virtuoso databases, the GPU tunnel and the demo. Everyone else codes on their own computer and pushes branches.

| Service | Where | Notes |
|---|---|---|
| Colima (Docker VM) | — | 12 CPUs, 28 GB RAM, 200 GB disk |
| Virtuoso **dev** | `http://localhost:8890/sparql` | 2,000 patients, container `hackathon-virtuoso` |
| Virtuoso **perf** | `http://localhost:8891/sparql` | 100,000 patients, container `virtuoso-perf` |
| GPU tunnel | `localhost:18000` | Reconnects by itself |
| API + demo | `http://localhost:8080` | Runs `main` with the LLM backend |

The control panel is [scripts/runbox.sh](scripts/runbox.sh):

```bash
scripts/runbox.sh up                                       # start what is not running
scripts/runbox.sh status                                   # what runs
scripts/runbox.sh restart                                  # after git pull on main
scripts/runbox.sh down                                     # stop API and tunnel (Virtuoso keeps running)
scripts/runbox.sh test --branch guard/my-fix               # a teammate's branch, in its own worktree
scripts/runbox.sh eval --execute --branch guard/my-fix     # eval with the LLM + execution on dev
```

- Branches are checked out under `../nl2sparql-worktrees/`, so the running demo is never disturbed.
- Logs are in `logs/`.
- After a reboot: `colima start`, then `scripts/runbox.sh up`. The perf database starts with `docker start virtuoso-perf`.
- **If dev Virtuoso answers HTTP 503**, slow queries from an eval are still running there: a client timeout does not stop a query on the server. Use `docker restart hackathon-virtuoso`. The data persists, and it answers again after about 3 minutes.

## 10. The knowledge base and the data files

**How the data is organized in Virtuoso:**

- **One patient = one named graph**, e.g. `http://example.org/graph/gen_mock_data-1_1012502`. Count patients with `COUNT(DISTINCT ?g)`. Exposing `?g` exposes the patient.
- **The ontologies** (the SPHN schema plus ICD-10-GM, CHOP, SNOMED CT, LOINC, UCUM, ORDO, GENO, SO and OBI, 13.0M triples) are loaded into one graph, `https://biomedit.ch/rdf/sphn-resource/global`, which is also the RDFS inference rule set.
- **A code is a node typed with a terminology class**: `?dx sphn:hasCode ?code . ?code a icd-10-gm:E11`. Codes in the data carry a year (`icd-10-gm/2022/A00.1`), so matching them needs `OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")` on that pattern.

**The two databases:**

- **Dev:** 10.8M patient triples. Ontologies load in about 4 minutes; patients in 14 s.
- **Perf:** 611.7M patient triples. Loading took 79 minutes.
- Setup is in [infra/virtuoso/README.md](infra/virtuoso/README.md). Always query over HTTP: the release's `query.sh` (isql) rejects some valid SPARQL.

**Data files** (all built from the dev database, none containing terminology content):

| File | What | Committed | Rebuild |
|---|---|---|---|
| `data/examples.jsonl` | 15 official examples + the worked example from the brief | yes | `python scripts/import_examples.py --release-dir ../hackathon-virtuoso-release` |
| `data/property_kinds.json` | What each of the 199 properties points to: value, value set or link. The guard relies on it | yes | `python scripts/profile_data.py` (about 2 minutes) |
| `data/schema_cards.json` | The 164 classes present in the data (the schema defines 214), with their properties: datatype, every class a link reaches, the terminology of each coded property (ICD-10-GM, CHOP, SNOMED CT or only the generic `sphn:Code`) and the members of each sphn-individual value set, all measured on the data | yes | `python scripts/profile_data.py --cards-only` (1 minute) |
| `data/team_examples.jsonl` | 3 retrieval-only examples the team wrote (a two-cohort "with cholera vs other patients" comparison, a conditional count with its share, a breakdown with a decade bucket, a braced UNION and `VALUES (?concept ?label)`), each verified on the dev database (row counts in `note`). Loaded by the example bank next to `examples.jsonl`; the eval scripts never read it | yes | by hand; verify on dev before adding |
| `assets/terminology_index.jsonl` | 38,765 code IRIs with their labels (ICD-10-GM, CHOP, the SNOMED CT concepts used in the data) plus English aliases for the codes the examples use (chop 00.0 therapeutic ultrasound, A00 and its subcodes, E11) | **no**, restricted | `python scripts/build_terminology_index.py` (17 s) |
| `assets/value_sets.json` | The coded values each class property uses in the data with their labels: 61 properties, coded sets of at most 20 members (civil status, tobacco use, consent, procedure intent, tumour grade) and every sphn-individual set in full; the schema cards show them | **no**, restricted | `python scripts/build_value_sets.py` (1 minute) |
| `data/guard_corpus.jsonl` | 37 queries the LLM really wrote, with labels and the guard's verdict | yes | `python scripts/export_guard_corpus.py` (run machine, after LLM runs) |

The full guide to the data, with examples, is [docs/DATA.md](docs/DATA.md).

Both `assets/` files are read once, when the API starts. They are optional: without them the query agent gets no code candidates or values, and the API still works. Build them on any machine that has the dev database.

The terminology-index builder also reads the optional local file
`assets/english_terminology_labels.jsonl`. Each line must contain
`{"system": "ICD-10-GM"|"CHOP", "code": "...", "labels": ["..."]}`.
This file and the generated index are restricted and must never be committed.

A question word matches a label on its own only when it is the whole label or the index shows the other words go with it (co-occurrence in at least 75% of at least two independent entries): "diabetes" finds "Diabetes mellitus", "hospital" no longer finds "Death in hospital". The subcodes of a matched code follow it (A00 → A00.0, A00.1, A00.9).

Every example record carries a `shape` tag (`plain`, `conditional_count`, `two_cohort`, `breakdown`; `scripts/import_examples.py` keeps them). BM25 cannot see shapes, so `ExampleBank.search` puts one `conditional_count` example in the last context slot when the question says how often / how many of / share / percentage / proportion / what fraction / what usually happens, and one `two_cohort` example for than other patients / compared with / versus.

## 11. Evaluation and baseline numbers

Four evals, all run on the run machine (`set -a && source .env && set +a` first):

```bash
python eval/run_eval.py --execute                     # whole pipeline, 31 shared questions, one at a time
python eval/run_eval.py --intent-cases --concurrency 4  # whole pipeline, 198 labeled questions: the verdict metric
python eval/run_intent_eval.py --concurrency 4        # the intent agent alone, 198 questions
python eval/run_query_eval.py --concurrency 4         # the query agent against 87 reference SPARQL queries (dev Virtuoso)
python eval/run_query_eval.py --rescore eval/reports/query-<date>.json   # execute again, no model calls
```

**Questions:**
- **31 shared:** the 16 official examples (`data/examples.jsonl`) plus 15 team questions ([eval/questions.jsonl](eval/questions.jsonl)).
- **198 labeled:** the 31 above plus 167 intent cases ([eval/intent_questions.jsonl](eval/intent_questions.jsonl)): paraphrases, traps, prompt injection, look-alikes, gray zones, style variants and three languages. 125 of them were written by an agent that never saw our prompts.
- **87 with a reference query:** the 11 compliant official examples plus 76 questions ([eval/query_questions.jsonl](eval/query_questions.jsonl)) whose reference SPARQL was verified on the dev database (45 dev, 31 holdout among the 76).

**Rules of the evals:**
- **Splits.** Questions are split into **dev** (may be studied one by one to improve prompts or context) and **holdout** (a blind exam: reported only as totals, never studied). The holdout numbers are our best guess for the jury's hidden list.
- **Fair test.** An official question never sees its own example in the context.
- **Reports** go to `eval/reports/` (git-ignored) and show up in the agents console (`/agents`, **Measurements**). [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md) explains every field.

**Whole pipeline, 31 shared questions, one at a time:**

| Backend | Verdicts correct | Forbidden marked compliant | Latency p50 / p95 | Tokens per request |
|---|---|---|---|---|
| `stub` (no model) | 87% | 0 | 13 ms / 53 ms | 0 |
| Qwen3.5-9B, 16 October (prompts v1, no codes) | 90% | 0 | 2.8 s / 16.6 s | ~3,000 |
| **Qwen3.5-9B, 17 October (merged system)** | **90.3%** | **0** | 3.1 s / 19.2 s | ~3,700 |

**Whole pipeline, 198 labeled questions (17 October, 4 at a time):**

| System | Split | Verdicts correct | Forbidden marked compliant | Allowed blocked |
|---|---|---|---|---|
| prompts v4, cap 1,500 (13:20; a second identical run gave 94.6% / 87.8%) | dev (149) | 94.0% | 1 of 88 | 8 of 61 |
| | **holdout (49)** | **91.8%** | 2 of 29 | 2 of 20 |
| + the 17 October fixes (judge checks and repairs, guard policy, one retry per checker, the less bad attempt kept, cards, retrieval; run 3 of 3) | dev (149) | 97.3% | 0 of 88 | 4 of 61 |
| | holdout (49) | 93.9% | 2 of 29 | 1 of 20 |
| **+ round 2 and the dev references as examples, Qwen3.6-27B for both agents** (21:00; p50 5.3 s, p95 17.9 s, 3,972 tokens) | dev (149) | **99.3%** | **0 of 88** | **1 of 61** |
| | **holdout (49)** | **100%** | **0 of 29** | **0 of 20** |

Two identical runs differ by up to 4 holdout points (2 of 49 questions), so a change is judged on all 198 and on which questions flip; the three runs of the fixed system gave 87.8 / 91.8 / 93.9% on the holdout and 96.6 / 95.3 / 97.3% on dev. The full log with every run of the day is [docs/PERFORMANCE.md](docs/PERFORMANCE.md); the case-by-case analysis behind the fixes is [docs/FAILURE_ANALYSIS.md](docs/FAILURE_ANALYSIS.md).

**The model change (17 October, 21:00).** Same pipeline, Qwen3.5-9B → Qwen3.6-27B: right numbers on the 87 reference questions 29–30 → **48** (blind holdout 10 → **19 of 31** in three runs, official examples 1 → 1 of 11), queries that run 85–87% → 92%, judge passes on the first try 45% → 83%, Virtuoso errors 11–13 → 7, p50 8.2–8.7 → 7.2 s, median query tokens ~5,900 → 3,325; intent agent on the holdout: 100% of forbidden requests detected, 0% false alarms. Reasons and reports: [docs/MODEL_HISTORY.md](docs/MODEL_HISTORY.md).

**Per agent (17 October):**
- **Intent agent (prompt v4), holdout:** 93% of forbidden requests detected, 5% false alarms, resists prompt injection, 631 tokens per call; identical numbers in three runs.
- **Query agent (prompt v4, 1,500-token cap), 87 reference questions, before and after the fixes (run 3):**
  - 4 → 6 return exactly the reference result, 4 → **18** return the reference's numbers (`exact_numbers`: other labels, rounding or layout);
  - 67% → **78%** run on Virtuoso (68% → 81% on the holdout); Virtuoso errors 29 → 19;
  - 9 → **6** allowed questions end up blocked;
  - the price: the judge rejects 57 of 87 first tries (29 for syntax, the model's own) and asks for a retry, so query tokens go from 3,345 to about 7,000 per question and p50 from 6.6 to 8.6 s; "valid on the first try" (62% → 34%) now measures the stricter judge, not the model.
- **Load test (17 October):** 32 requests, 8 at a time, against the API with the real model: all HTTP 200, p50 3.7 s, p95 11.2 s, 1.33 requests per second, 2,873 tokens on average.

  Of the 13 compliant queries in the 31-question run, 8 return rows, 2 return nothing and 3 fail in Virtuoso.

**Why allowed requests end up blocked (dev, 12):**

| Cause | Requests | Owner |
|---|---|---|
| The query does not parse, so the guard cannot check it. Two of these answers were cut off at the 900-token cap | 7 | B |
| The query groups by the code node of each record instead of the concept (e.g. `GROUP BY ?code` for "the 10 most common diagnoses"). The guard flags it correctly, and the retry does not fix it. Since 17 October the guard's message says how (`?x sphn:hasCode ?code . ?code a ?concept . GROUP BY ?concept`) and the retry sentence no longer suggests a single row | 4 | B, plus D for a clearer retry hint |
| Intent false alarm ("How many patients have at least one identifier recorded?") | 1 | B |

**Official example timings** (one run each on the run machine; dev: server time through isql, #06 over HTTP; perf: HTTP round trip):

| Example | Dev, 2,000 patients | Perf, 100,000 patients |
|---|---|---|
| #01 cancer pathways | 0.08 s | > 30 min (timeout) |
| #02 cholera drug durations | 0.5 s | 114 s |
| #03 cholera by sex | 1.4 s | 28 min |
| #04 born abroad | 2.0 s | > 30 min (timeout) |
| #05 prescribed vs actual route | 25.6 s | > 30 min (timeout) |
| #06 therapeutic ultrasound | 0.15 s | 144 s |
| #07 how drugs are given | 4.1 s | 1.3 s |
| #08 genetic variants | 0.01 s | 4.1 s |
| #09 adverse events | 0.05 s | 0.1 s |
| #10 preterm births | 0.15 s | > 30 min (timeout) |

Query performance is a major lever: even the organizers' own queries can take minutes at 100k patients on this machine.

## 12. Things we learned the hard way

1. **The 2026.1 schema does not match the data.** The schema says `sphn:hasRankCode`; the data uses `sphn:hasRank` with `sphn-individual:Principal`/`Secondary`/… The data wins. The judge flags terms absent from the data and suggests the right one.
2. **Codes carry a year.** Without the inference option, `icd-10-gm:A00` matches nothing.
3. **ICD-10-GM and CHOP labels are in German and French**, while requests come in English. SNOMED labels are English.
4. **The free text in the mock data is lorem ipsum, and 38% of patients have cholera.** It is synthetic: do not read medical meaning into the numbers.
5. **`query.sh` (isql) rejects valid SPARQL**: examples #06 and #15 fail there and work over HTTP.
6. **Qwen3.5 must run with thinking off** (see [section 8](#8-the-llm-on-the-gpu)).
7. **rdflib quirks that matter to the guard:**
   - Aggregate nodes are named `Aggregate_Count`, `Aggregate_Sample`, … with no trailing underscore.
   - Projected `GROUP BY` keys become `SAMPLE(...)`.
   - `GROUP BY (expr)` without `AS` loses the expression.
   - `__agg_N__` names repeat across subqueries.
   - `pyparsing` must stay pinned at 3.2.0.
   - `translateExists` stores the translated `EXISTS` / `NOT EXISTS` pattern as an attribute (`node.graph`); `node.items()` keeps the raw parse tree without its inner filters. Walk `exists_pattern(node)` (`guard/provenance.py`), never the items: until 17 October everything inside `NOT EXISTS` was invisible to the guard.
8. **Passing our parser does not mean Virtuoso accepts the query.** Two LLM queries failed only in Virtuoso. We may not ask the database at runtime, so the fix has to come from prompts and style rules.
9. **The SIB `sparql-examples` repo has no SPHN examples.** It covers UniProt, Bgee, Rhea and the like. Reuse its format, not its content.
10. **Vast's suggested SSH command forwards port 8080**, which clashes with our demo. Use another local port, e.g. `-L 8888:localhost:8080`.
11. **rdflib's SPARQL parser is not thread-safe.** The orchestrator runs the judge and the guard in worker threads, and two requests parsing at once corrupted the parser for the rest of the process: every later request came back "not compliant". With 4 concurrent requests, 198 of 198 failed. `parse_sparql` now takes a lock, and a test parses from 8 threads at once.
12. **A client timeout does not stop a Virtuoso query.** Slow queries from an eval kept running and saturated dev Virtuoso (HTTP 503). The query eval runs queries one at a time and can `--rescore` a report. Restart the container if it happens (section 9). The client raises `VirtuosoTimeout` on HTTP 206 / `X-SQL-State: S1TAT` (a partial result from the anytime `timeout` parameter) instead of parsing wrong partial aggregates, and `eval/time_on_perf.py` no longer sends that parameter: it times plain runs with a client deadline (`--timeout-ms`) and restarts the perf container after every overrun.
13. **On our Vast instance, `supervisorctl restart vllm` keeps the old server alive** on port 18000. Kill it before starting the new model (section 8).
14. **A bigger model did not fix the SPARQL, until the pipeline could use it.** On 17 October morning Qwen3.5-9B, Coder-14B and Coder-30B-A3B gave the same exact-result rate. The same evening, with the judge's repairs and the dev reference queries as examples, Qwen3.6-27B took the blind holdout from 10 to 19 of 31 right numbers (section 8). Re-test the model whenever the pipeline changes a lot.
15. **90% correct verdicts can hide poor SPARQL.** Measure query results against reference queries (`eval/run_query_eval.py`), not only verdicts.
16. **Do not commit environments or editor settings.** A teammate's `.venv311/` (1,834 files) almost got merged. `.venv*/` and `.vscode/` are git-ignored now.
17. **A Virtuoso quirk in conditional counts.** `OPTIONAL { GRAPH ?g { … } BIND(… ) }` used for a conditional count returns zeros when the same query also has an `EXISTS` cohort `BIND`, whatever the order. Use `IF(?concept = …, ?x, ?none)` binds instead (reference query q-075).
18. **The mock data is thin outside cholera.** Billed ICD codes are almost all cholera (778 patients) plus the chapter code (189); other codes have fewer than 10 patients; CHOP is almost all `00.0x`; many SNOMED value sets are random. Questions about other diagnoses return tiny or empty results on both databases.
19. **rdflib's syntax message names the wrong token.** "Expected SelectQuery, found 'GRAPH'" for a missing parenthesis three lines later, and the model repeats the error on its retry. The judge now rewrites a standard prefix declared with another namespace (`fix_standard_prefixes`), repairs three mistakes with exactly one meaning without a model call (`GROUP BY true`, `GROUP BY` inside the WHERE braces, a UNION branch that is a bare `GRAPH` block) and replaces rdflib's message with hints that name the real error and say what to write instead (`judge_checks/syntax.py`). On the 17 October dev run: 13 of 52 unparseable answers repaired, 36 explained; 0 hints on the examples and the dev references.
20. **The anytime `timeout` parameter changes Virtuoso's plan.** With it, an OPTIONAL + inference query that answers in 84 ms runs to the deadline and returns HTTP 206 with wrong partial aggregates. Time plain runs.
21. **The query agent can drop the concept the question names.** "How many patients have cholera on a hospital bill, by sex?" came back as all patients by sex, and no check compared the question with the query. The judge now takes the codes the question names strongly and asks for the missing pattern when the query uses neither the code nor a subcode or parent code (`judge_checks/coverage.py`). A code counts when the question writes the ICD code out, or when all the words of an ICD-10-GM or CHOP label stand side by side in the question; SNOMED CT categories never count, nor does a word that only implies its label. With the real index: 0 hits on the 16 official examples, the 3 team examples and the 45 dev references; over all 2,202 dev queries of the 29 saved reports, 40 hits, none on a right answer.
22. **A judge hint buys a whole new query, which comes back worse as often as better.** On the 17 October run the record-name hint sent q-003 to `SELECT ?stays ... GROUP BY ?stays` in 3 of 3 runs and q-042 to a UNION that Virtuoso rejected; the model is deterministic, so a hint that misleads misleads every time. Shapes with exactly one correct form are now repaired before the checks run, with no model call (`judge_checks/rewrites_scope.py`, `rewrites_aggregation.py`): a sub-select that hides variables an outer clause reads (exported to its SELECT and GROUP BY), `COUNT(?prescription)` next to the typed `?drug_prescription` (renamed), a record link's object typed with a terminology IRI (`?intent a snomed:X` becomes `?intent sphn:hasCode ?intent_code . ?intent_code a snomed:X`), a VALUES block on a code node (the node is typed and the VALUES moves to the concept), `FILTER(?pid = ?r/sphn:hasSubjectPseudoIdentifier)` (a division, dropped), `?date <= (bif:datediff(...) <= 90)` (the inner comparison is kept); a top-level GROUP BY per patient or per record under an aggregate-only SELECT (dropped), `COUNT(DISTINCT ?g)` under a record name (counts the one typed record whose class shares a word stem with the alias), a GROUP BY key bound only by labels inside OPTIONAL (`FILTER (BOUND(?key))`), a BIND alone in an OPTIONAL (moved out), `ROUND(1000.0 * AVG(?x)) / 10` on a plain statistic (factor 100 removed), a subcode regex copied from an example on a code already typed concretely (removed with its chain). Each rewrite re-parses its result and gives up when it does not parse or projects other columns. 0 hits on the 16 official examples, the 3 team examples and the 45 dev references; replayed on the 17 October dev answers, q-020, q-022, q-042, q-050, q-054, q-060, q-064 and q-073 become right with no model call.
23. **The model follows a judge hint to the letter, so a hint must name the query's own variables.** The generic templates of the hints were copied verbatim: `?x sphn:hasCode ?c ... VALUES` replaced the right property (q-047) and invented a category (q-035), `BIND(?g AS ?x)` turned record columns into patient counts (q-011, q-074, q-076), a "write `hasConsequences`" recipe changed the cohort (official-09) and `DeathDate.hasYear` (random values in the data) emptied q-005. The hints in `judge_checks/scope.py`, `semantics.py` and `schema.py` are built from the query itself: `SUM(IF(?count_diagnoses > 1, 1, 0))` next to a per-patient sub-select, the query's own record, code property and codes as the VALUES to group by, `BIND(?record AS ?x)` for a column named after records, "add ?case to the sub-select's SELECT" for a hidden variable, only the linking record's dateTime for DeathDate/BirthDate, and the plain value list when a value-set member belongs to another property of the same class. Six further checks catch a statistic scaled like a percentage, a percentage whose constants do not make 100, a decade computed from `hasValue` but compared with calendar years, a share that is 100% by construction, a patient count over the backbone while every measure comes from an OPTIONAL block, and a GROUP BY key bound only in an OPTIONAL linked by `?g` alone. 0 hits on the 64 reference queries; on the 17 October dev run 31 of 94 attempts get a different hint, none of them a right answer.
24. **"How many prescriptions ..." comes back as a patient count.** q-049 returned the patients only, q-042 counted patients under `?episodes`, q-003's retry projected an unbound `?stays`: the asked record count was missing or on the wrong unit in ten dev answers, all wrong. The judge now reads the question (`judge_checks/coverage.py`, `check_record_count_asked`): when "how many", "the number of" or "give" is followed by at most two modifiers and a plural record noun, and no COUNT in the query counts records, it asks for `(COUNT(DISTINCT ?prescription) AS ?drug_prescriptions)` next to the patient count, naming the query's own record variable. The record nouns come from the cards (the head word of every class with `hasSubjectPseudoIdentifier`) plus the everyday plurals the class names do not spell (stays, admissions, visits, tests, records); a patient word between cue and noun or a kind word ("how many different procedures") switches it off. 0 false alarms on the 64 reference queries (the cue matches 25 of them, all counting records); 10 hits on the 17 October dev finals, all wrong.
25. **The model copies what a card shows.** The card line `hasCode -> SNOMED CT code: ?c a snomed:<code> OPTION (inference ...)` was copied as it stood in 11 dev answers of run 3 (all unparseable), and the `hasSubjectPseudoIdentifier` link every card advertised became a join in 5 dev answers, wrong or harmful in 3, although no compliant reference joins through it. Cards now say `hasCode -> SNOMED CT code (?c a ?concept; one code from the candidates to match a concept)` with no copyable code and no inference option (the prompt rules carry it), and show the pseudo-identifier link only on the SubjectPseudoIdentifier card, which the picker brings in only when the question names identifiers. The picker also learned early/preterm (GestationalAgeAtBirth), "give drugs"/route/by mouth/IV (DrugAdministrationEvent), in hospital/inpatient (AdministrativeCase) and intent/guidance/screening/diagnostic (Intent); a code candidate that is a member of a class's value set pulls that class through a shown card's link; a candidate whose label only restates a class name ("Imaging (procedure)") is dropped. Card coverage of the classes the references use: 103 → 106 of 107; value sets capped at 20 members instead of 15 (adds Intent and TumorGradeAssessment).
26. **A guard hint bought a worse regeneration too.** Told to "group by a category", the model invented categories and codes and ran out of tokens (q-013, q-071, official-07); a parse error got rdflib's message plus a privacy sentence, and 0 of 6 such retries came back parseable; an outer SELECT that re-projects sub-selects of totals (q-052's share) was rejected as "individual rows" in 3 of 3 runs. The guard now sees through re-projected sub-selects (not a UNION: gen-024 is caught only there), a patient-graph key gets the per-patient sub-select recipe, a code node "never list codes you were not given", an all-totals query "remove GROUP BY", and a parse error the judge's diagnosis (`docs/GUARD.md`). Measured on 405 dev and official queries: 2 verdict changes (q-052), 0 rule-set changes.

## 13. Next steps per module

Most useful first. Measure every change with the evals in [section 11](#11-evaluation-and-baseline-numbers) and compare with the numbers there.

**A · Integration** (David)
- The load test ran on 17 October (section 11). Repeat it after any change to the orchestrator.
- The organizers run our repository on their machine ([section 6, "For the jury"](#for-the-jury-the-full-system-on-your-machine)). Still open: whether their machine serves the model or calls ours; ask them, and hand over our endpoint and token with the submission if needed.
- Install from scratch in a clean environment, then run the tests and the HTTP smoke checks in [section 6](#6-getting-started-on-your-computer).

**B · Agents and prompts** (Santiago). Full backlog in [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md#9-backlog-for-module-b-most-useful-first).
- Two answers (#01, #08) are still cut off at 1,500 tokens: those questions need a shorter query shape, not a bigger cap.
- Five allowed reference questions are still blocked; the query eval report shows each query and reason.
- Fix the intent false alarm on counts that mention identifiers.

**C · Data and context** (Santiago)
- Add the 21 dev reference queries to the example bank. Measure only on the holdout.
- Add English labels for ICD-10-GM and CHOP codes (`assets/english_terminology_labels.jsonl`; the builder already reads it). An alias layer covers the codes the examples use (therapeutic ultrasound, cholera and its subcodes, E11); the rest of the labels are German and French.
- Set a server-side query timeout in dev Virtuoso, so evals cannot saturate it.

**D · Guard and judge** (Adrika, Gayathri)
- gen-106 (a graph-name filter marked compliant) was a retry-policy leak, fixed on 17 October: an R1 violation is never retried. Two holdout leaks remain unexamined on purpose.
- Catch Virtuoso-only errors in code before the guard. The patterns are in [docs/SPARQL_LLM_VALIDATOR.md](docs/SPARQL_LLM_VALIDATOR.md). Consider SIB's validator, offline.
- Check whether `GROUP BY ?g` inside a subquery that is aggregated afterwards is a false alarm (official #10).

**E · Eval and demo**
- Build a 10-run timing harness on perf: query performance is a main criterion.
- Polish the demo and prepare the numbers for the report and the video.

## 14. Questions for the organizers

**Answered:** database-agnostic, execution optional, return the query of forbidden requests for judging (see [section 2](#2-what-the-jury-told-us)).

**Still open** (most important first):

1. How will they run our system: from the repo on their hardware (which one?), or by calling an endpoint we host? How do we keep GPU access during judging?
2. Do they check that the query returns the *correct* result, or only the verdict and the timings?
3. Only `SELECT`? The API spec and the rules disagree; we assume `SELECT` only.
4. How do they time queries: over HTTP or with `query.sh`? Warm or cold cache? Is there a timeout? How many patients?
5. Gray zones: is a `GROUP BY` on fine attributes (birth date), or a `MIN`/`MAX` that points at one patient, non-compliant? Is a minimum group size required?
6. Will the benchmark include adversarial requests (prompt injection)? Is it English only?
7. Do their reference queries follow fixed conventions? For example, does "diagnosed with X" mean BilledDiagnosis or any diagnosis class?
8. Are there more SPHN example queries than the 15 in `queries-example.md`?

## 15. Team rules

1. **One branch per module and topic**, named `integration/…`, `agents/…`, `data/…`, `guard/…` or `eval/…`. Merge into `main` through a pull request; GitHub runs the tests on every pull request.
2. **Before asking for a merge:** `python -m pytest -q` passes, and the eval on the run machine does not get worse. Paste the eval summary in the pull request.
3. **Every bug fix comes with a regression test.** See `GROUPING_CASES` in [tests/test_guard.py](tests/test_guard.py).
4. **Shared interfaces** (`models.py`, `agents/base.py`) change only by agreement.
5. **Never commit** `.env`, tokens, GPU host details, ontology files, `assets/` or patient data. The repo becomes public.
6. **Keep the pipeline database-agnostic and the models open source.**
7. **Code, comments and documents in English.**
8. **Using AI assistants is fine.** Keep track of how you use them: the report must disclose it.

## 16. Submission checklist

By **Sunday 18 October, 11:00**:

- [ ] Code pushed to the organizers' repo `NW_26_<Company>_<Team>`. One member is added as collaborator and invites the rest.
- [ ] README for the jury: overview, how to run, dependencies, **full names and roles of all members**.
- [ ] `pip install -r requirements.txt` works in a clean environment, and `python -m nl2sparql` starts without debugging.
- [ ] Report, **3 pages maximum**: title, approach (methods, architecture, why), results (metrics, figures), challenges (including ideas that did not work), and use of AI agents and LLM coding assistants.
- [ ] Video, **2 minutes maximum**, watched before the report: goal and relevance, key results, a demo. Share it as a SwissTransfer link.
- [ ] External sources credited, nothing restricted or secret in the repo.
- [ ] A 5-minute presentation ready, in case we win.

## 17. Glossary

| Term | Meaning |
|---|---|
| RDF triple | One fact: subject · predicate · object |
| Named graph | A named group of triples. Here, one patient |
| SPARQL | The query language for RDF |
| Virtuoso | The RDF database (triplestore) used by SPHN hospitals and by us |
| SPHN schema | Defines the classes (`BilledDiagnosis`, `LabTestEvent`, …) and properties (`hasCode`, …) |
| SHACL | Rules describing the shape each class must have |
| ICD-10-GM / CHOP | Swiss codes for diagnoses / procedures |
| SNOMED CT / LOINC / UCUM | Clinical concepts / lab tests / units |
| Inference rule set | Makes a query for a parent code also match its child codes |
| vLLM | The server that runs the open-source LLM and exposes an OpenAI-compatible API |
| Vast.ai | Where we rent the GPU |
| Colima | Runs Docker on the Mac |
| Worktree | A second checkout of the repo, used to run a teammate's branch |

## 18. Team

| Full name | Role | Modules |
|---|---|---|
| Santiago Figueroa Luchetti | Architecture and backend | Architecture, B (agents and models), C (data and context), run machine |
| Adrika Pradhan | Backend | D (judge), C groundwork (loader validation, retrieval) |
| Gayathri Girish | Backend | D (compliance guard) |
| David García Costa | Backend | A (integration) |
| Odinaka Ogbujiagba | Frontend | E (demo and frontend) |

## 19. Add-ons from the team (17 October evening)

Merged on the run machine on 17 October at night. This README stays the team's master document; the add-ons sit on top of the pipeline and change neither `POST /nl-to-sparql` nor the agents, the judge or the guard.

**Gayathri (branch `compliance`, pull requests #6 and #7):**
- **"Did you mean...?"** A vague question ("patients with diabetes") gets the coded choices first, from the terminology index and without a model call: `POST /demo/clarify`, `src/nl2sparql/assist.py`.
- **Follow-up questions.** An answer that is one number of at least 2 offers ways to break it down (by sex, by year...) as buttons; the follow-up's answer is read out in one sentence. Field `follow_ups` of `/demo/ask`; never for a blocked or withheld result.

**Adrika (branch `features-adrika`):**
- **Result display** (`frontend/js/result.js`, `policy.js`): numbers, bars, grouped comparisons, exact tables, pagination and truncation notices, driven by column metadata from `src/nl2sparql/demo_results.py`.
- **Privacy controls for the demo:** whole-result suppression below `DEMO_MIN_GROUP_SIZE` (off by default, waiting for the organizers), a separate public console mode (`DEMO_PUBLIC_MODE`), `Cache-Control: no-store` on demo pages. Review: [docs/PRIVACY_REVIEW.md](docs/PRIVACY_REVIEW.md).
- **Research workflow:** CSV and clipboard export, saved snapshots, a reproducibility card (question, time, dataset identity; never the query) and opt-in shared links that expire after seven days (`POST /demo/shares`, `src/nl2sparql/demo_shares.py`).
- **Verification:** `eval/validate_demo_results.py` checks the console's answers against the reference results on a live API and database ([docs/RESULTS_VALIDATION.md](docs/RESULTS_VALIDATION.md)); Node tests (`tests/frontend/`) and Playwright browser tests on desktop and phone (`tests/browser/`, run as in [docs/features/README.md](docs/features/README.md)).
- **CI/CD and container:** `.github/workflows/tests.yml` runs pytest, the frontend and browser tests and builds the container for AMD64 and ARM64 on every pull request; **on a push to `main` or a `v*` tag it publishes the image to GitHub Container Registry**. The `Dockerfile` copies only `src/`, `frontend/` and the public data files (`.dockerignore` denies everything else, `assets/` and `.env` included). Setup and tags: section 19 of [docs/operations/PROJECT_HANDBOOK.md](docs/operations/PROJECT_HANDBOOK.md#19-cicd-and-container-delivery).
- **Documents:** [docs/README.md](docs/README.md) (index), [docs/features/README.md](docs/features/README.md) (her handoff), [docs/FRONTEND.md](docs/FRONTEND.md) (response contracts), and [docs/operations/PROJECT_HANDBOOK.md](docs/operations/PROJECT_HANDBOOK.md): a copy of this README as of 17 October with the CI/CD section; this README is the one kept up to date. `todo.md` moved to `docs/plans/PROJECT_STATUS_2026-10-17.md`.

**How they were fitted to the project:**
- `DEMO_SHOW_DETAILS` (default `true`) keeps the team console as it was: it shows the generated query of an allowed request, the specific reason and the suggested rephrase of a blocked one, and the retry diagnostics. `false`, or the public mode, gives Adrika's hardened answers: no query, a generic reason, no diagnostics, generic errors. The tests cover both.
- The follow-up buttons appear under Adrika's result display, and only for a released result.
- One add-on test paired a cholera question with a diabetes query; the judge's coverage check rightly asked for a retry, so the test now uses a cholera query.
- **Known gap:** the container leaves out `eval/query_questions.jsonl`, so inside it the 45 dev reference queries are not retrieval examples (`CONTEXT_DEV_REFERENCES` does nothing there). They are part of what gives 19 of 31 on the blind holdout; copy that file into the image before the jury uses it.
