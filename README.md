# NL-to-SPARQL Gateway

**SIB / SPHN challenge · Neural Wave hackathon 2026 · team master document**

A researcher asks a question in English. We translate it into a SPARQL query over SPHN clinical data and decide whether that query respects the privacy rules. Everything sits behind one endpoint, `POST /nl-to-sparql`, uses only open-source models, and never touches a database at request time.

This README is the single source of truth for the team. Read it once end to end before you start, then use the contents to jump around. If you work with an AI coding assistant, it also reads [AGENTS.md](AGENTS.md).

*Last updated: Saturday 17 October 2026, 01:00. Deadline: **Sunday 18 October, 11:00**.*

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
| Every component of the design | Built and wired end to end; the test suite runs locally and in pull-request CI |
| LLM | Connected: Qwen3.5-9B served by vLLM on a Vast.ai RTX 5090 |
| Quality (31 test questions) | 90% verdicts correct, **0 forbidden requests marked compliant**, 2.8 s median latency |
| Knowledge base | Two local Virtuoso databases: dev (2,000 patients) and perf (100,000 patients) |
| Demo page | Works: `http://localhost:8080` on the run machine |
| GitHub | Being pushed; the official `NW_26_<Company>_<Team>` repo is pending |
| Medical-code index | Script ready, index not built yet (module C) |
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
5. **One retry per request, shared** by the judge (syntax or unknown terms) and the guard (an allowed request whose query broke a rule).
6. **A forbidden request is never retried.** A retry could turn it into a clean query, which we would then wrongly mark compliant.
7. **A query that breaks a rule is never marked compliant**, even if that costs us a point.

### How the guard decides

| Intent agent (request) | Query check | What happened | Verdict |
|---|---|---|---|
| OK | OK | Normal case | compliant |
| flagged | broken | Both agree the request is forbidden | non-compliant, no retry |
| flagged | OK | The query agent cleaned up a forbidden request | non-compliant, no retry |
| OK | broken | The query agent made a mistake on an allowed request | 1 retry naming the broken rule; still broken means non-compliant |

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
│   ├── judge.py                  syntax, prefixes, terms absent from the data             (D)
│   ├── sparql/                   Virtuoso-aware parsing shared by guard and judge         (D)
│   ├── virtuoso.py               small client (scripts, eval, demo only)                  (C)
│   └── demo/index.html           demo page                                                (E)
├── data/                         committed data files (no restricted content)             (C)
├── scripts/                      import examples, profile data, terminology index, runbox (C)
├── infra/virtuoso/               how to run the two Virtuoso databases                    (C)
├── eval/                         questions + evaluation harness                           (E)
├── tests/                        unit tests, one file per module
└── docs/                         design spec and implementation plan
```

| | Module | Owns | Tests | Owner |
|---|---|---|---|---|
| **A** | Integration | API, orchestrator, factory, config, input check, load test, deployment | `tests/test_pipeline_and_api.py`, `tests/test_input_check.py` | David |
| **B** | Agents and prompts | `agents/`, `llm.py`, model choice | `tests/test_agents_and_judge.py` | Santiago (models) |
| **C** | Data and context | `context/`, `scripts/`, `data/`, `infra/`, `virtuoso.py` | `tests/test_context.py` | Santiago |
| **D** | Guard and judge | `guard/`, `judge.py`, `sparql/` | `tests/test_guard.py`, `tests/test_sparql.py` | Two teammates: judge, compliance guard |
| **E** | Eval and demo | `eval/`, `demo/`, report and video numbers | `python eval/run_eval.py` | |

**Shared interfaces**: [models.py](src/nl2sparql/models.py) and [agents/base.py](src/nl2sparql/agents/base.py). Change them only after agreeing with the team.

**Guides per topic:**
- [docs/DATA.md](docs/DATA.md): how the patient graphs look, with examples. For everyone.
- [docs/data-and-context/README.md](docs/data-and-context/README.md): context module checklist, split between local tests and real-graph validation.
- [docs/GUARD.md](docs/GUARD.md): how the compliance guard works, its tools and its backlog. For module D.
- [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md): the two LLM agents, their configuration, how to measure them, how to change a prompt or a model, and their backlog. For module B.

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

**Agents console:** <http://localhost:8080/agents> shows the full configuration of both agents and their prompts. It can:
- preview exactly what each model would receive for a question, without calling any model;
- run one question and show what each agent did (with the LLM backend, this calls the model);
- list every saved eval report.

See [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md).

## 7. Configuration reference

All settings come from environment variables; `.env.example` lists them. Load them with `set -a && source .env && set +a`.

| Variable | Default | What it does |
|---|---|---|
| `NL2SPARQL_BACKEND` | `stub` | `stub` (no model) or `llm` |
| `LLM_BASE_URL` | `http://localhost:8000/v1` | OpenAI-compatible server. Through the SSH tunnel: `http://localhost:18000/v1` |
| `LLM_API_KEY` | `not-needed` | Bearer token; needed for the public Vast endpoint, not for the tunnel |
| `LLM_QUERY_MODEL` | — | Model name served by vLLM, e.g. `Qwen/Qwen3.5-9B` |
| `LLM_INTENT_MODEL` | query model | A separate model for the intent agent |
| `LLM_INTENT_BASE_URL`, `LLM_INTENT_API_KEY` | shared values | Give the intent agent its own model server |
| `LLM_INTENT_EXTRA_BODY` | inherits `LLM_EXTRA_BODY` | Request fields for the intent model only; `'{}'` sends none |
| `LLM_INTENT_MAX_TOKENS`, `LLM_QUERY_MAX_TOKENS` | `200`, `900` | Answer length cap per agent |
| `INTENT_PROMPT_VERSION`, `QUERY_PROMPT_VERSION` | `v4`, `v2` | Which prompt version each agent uses (see `src/nl2sparql/agents/prompts.py` and docs/LLM_AGENTS.md §6) |
| `LLM_TIMEOUT_SECONDS` | `30` | Per LLM call |
| `LLM_VERIFY_TLS` | `true` | `false` for servers with a self-signed certificate |
| `LLM_EXTRA_BODY` | empty | JSON merged into every LLM request. For Qwen3.x: `'{"chat_template_kwargs": {"enable_thinking": false}}'` (keep the single quotes) |
| `VIRTUOSO_ENDPOINT` | `http://localhost:8890/sparql` | Dev only: scripts, `eval --execute`, the demo's dev check |
| `MAX_INPUT_CHARS` | `2000` | Longer requests get a 400 |
| `MAX_RETRIES` | `1` | Shared retry budget per request |
| `REQUEST_TIMEOUT_SECONDS` | `75` | Total time limit for one pipeline request, including agent calls and retries. Expiry returns HTTP 503; use a positive number of seconds |
| `RETURN_NONCOMPLIANT_QUERY` | `true` | Hackathon behavior. `false` = a forbidden request returns an empty `sparql_query` |
| `GPU_SSH_HOST`, `GPU_SSH_PORT` | — | Run machine only, for the tunnel (ask Santiago) |
| `API_PORT` | `8080` | Port used by `scripts/runbox.sh` |
| `NL2SPARQL_DATA_DIR`, `NL2SPARQL_ASSETS_DIR`, `NL2SPARQL_LOG_PATH`, `NL2SPARQL_REPORTS_DIR` | `data/`, `assets/`, `logs/requests.jsonl`, `eval/reports/` | Where data, built assets, the request log and the eval reports live |

## 8. The LLM on the GPU

| | |
|---|---|
| Provider | Vast.ai, on-demand instance rented by Santiago (about $100 of credit) |
| GPU | 1× NVIDIA RTX 5090, 32 GB |
| Server | vLLM 0.30 (Vast "vLLM" template), OpenAI-compatible API |
| Model | `Qwen/Qwen3.5-9B`: 9.7B parameters, Apache-2.0, not gated |
| Server settings | `--max-model-len 32000 --max-num-seqs 8` (at most 8 requests decoded at once) |
| Both agents | Same model, different prompts. Separate it only if the eval says so |

**Turn thinking off.** Qwen3.5 thinks by default. In our first test it spent its whole 600-token budget reasoning and returned nothing (7 s). With thinking off it answered in 1.2 s with 47 tokens. Always set `LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'`.

**How to reach it.**

- **SSH tunnel** (what the run machine uses): `ssh -N -p <port> -L 18000:127.0.0.1:18000 root@<host>`, then `LLM_BASE_URL=http://localhost:18000/v1` with no token. Santiago adds your SSH public key to the instance first.
- **Public endpoint:** `http://<host>:<external port>/v1` with `LLM_API_KEY=<instance token>`. Ask Santiago for both. Never commit them.

**Changing the model** is done on the instance by its owner. Per the instance's own guide (`/etc/vast-agents-guide.md`, which AI agents must read before acting on the instance):

1. Set both `VLLM_MODEL` and `MODEL_NAME` in `/etc/environment`.
2. Run `supervisorctl restart vllm model-ui`.

vLLM listens on `127.0.0.1:18000`; Caddy in front adds token auth. Do not add `--api-key` to vLLM.

**Candidates to compare later** (all Apache-2.0, not gated):

- `Qwen/Qwen2.5-Coder-14B-Instruct-AWQ` or `-GPTQ-Int8`: stronger at code, fits in 32 GB when quantized.
- `swiss-ai/Apertus-8B-Instruct-2509`: the model the organizers encourage, a good fit for the intent agent.
- `Qwen/Qwen3-Coder-30B-A3B-Instruct`: needs a bigger GPU.

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
| `data/schema_cards.json` | The 164 classes present in the data (the schema defines 214), with their properties | yes | same script |
| `assets/terminology_index.jsonl` | Code IRIs with their labels | **no**, restricted | `python scripts/build_terminology_index.py` |
| `data/guard_corpus.jsonl` | 37 queries the LLM really wrote, with labels and the guard's verdict | yes | `python scripts/export_guard_corpus.py` (run machine, after LLM runs) |

The full guide to the data, with examples, is [docs/DATA.md](docs/DATA.md).

The terminology-index builder also reads the optional local file
`assets/english_terminology_labels.jsonl`. Each line must contain
`{"system": "ICD-10-GM"|"CHOP", "code": "...", "labels": ["..."]}`.
This file and the generated index are restricted and must never be committed.

## 11. Evaluation and baseline numbers

```bash
python eval/run_eval.py                    # in-process, backend from the environment
python eval/run_eval.py --execute          # also runs compliant queries on VIRTUOSO_ENDPOINT
python eval/run_eval.py --url http://localhost:8080
python eval/run_intent_eval.py             # the intent agent alone: detection rate, false alarms, tokens
```

In-process pipeline runs also report, **per agent**:
- tokens and time;
- why retries happened, and the query each retry replaced;
- answers cut off at the token cap.

These appear in `per_agent` in the summary and in `diagnostics` in each row. The intent eval is the cheap way to compare intent models and prompts. The agents console (`/agents`, **Measurements**) shows every saved report side by side. [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md) explains every field.

- **Questions:** the 16 official examples (from `data/examples.jsonl`) plus team questions in [eval/questions.jsonl](eval/questions.jsonl). Each team question has `{id, question, expected_compliant, category}`.
- **Fair test:** by default an official question cannot see its own example in the context.
- **Reports** go to `eval/reports/`, which is git-ignored.

**Baseline to beat** (31 questions):

| Backend | Verdicts correct | Forbidden marked compliant | Latency p50 / p95 | Tokens per request |
|---|---|---|---|---|
| `stub` (no model) | 87% | 0 | 13 ms / 53 ms | 0 |
| `llm`, Qwen3.5-9B | **90%** | **0** | 2.8 s / 16.6 s | ~3,000 |

**Where the LLM run still fails:**

- **Wrong verdicts:** #01 and #08 (the model wrote invalid SPARQL, even after the retry) and #07.
- **Compliant queries executed on dev:** 9 of 13 return data, #06 and #09 return no rows, and #05 and #10 fail in Virtuoso.

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
8. **Passing our parser does not mean Virtuoso accepts the query.** Two LLM queries failed only in Virtuoso. We may not ask the database at runtime, so the fix has to come from prompts and style rules.
9. **The SIB `sparql-examples` repo has no SPHN examples.** It covers UniProt, Bgee, Rhea and the like. Reuse its format, not its content.
10. **Vast's suggested SSH command forwards port 8080**, which clashes with our demo. Use another local port, e.g. `-L 8888:localhost:8080`.

## 13. Next steps per module

**A · Integration**
- Decide where the API runs during judging and how it reaches the model. This depends on the organizers' answer in [section 14](#14-questions-for-the-organizers); no deployment mode is confirmed yet.
- Run the HTTP load test against the LLM backend on the run machine. The command below sends eight concurrent API requests. Each request can make both agent calls concurrently, so model calls may queue at vLLM's eight-sequence limit. The tool calls only `/health` and `POST /nl-to-sparql`; it does not execute SPARQL.

  ```bash
  python -m nl2sparql.load_test --url http://localhost:8080 \
    --questions eval/questions.jsonl --requests 32 --concurrency 8 --require-llm
  ```

  Record `invalid_or_failed`, p50/p95 latency, throughput and mean tokens. The command is ready, but the eight-request LLM load run is still pending.
- Install from scratch in a clean environment before submitting, then run the tests and the HTTP smoke checks in [section 6](#6-getting-started-on-your-computer).
- Finish the jury README after the deployment answer arrives and all teammates confirm their full names and roles in [section 18](#18-team). The stub instructions above are for local development; judging needs the configured LLM backend.

**B · Agents and prompts** (Santiago). Full backlog in [docs/LLM_AGENTS.md](docs/LLM_AGENTS.md#9-backlog-for-module-b-most-useful-first).
- Per-agent baseline on the GPU, then the Apertus comparison for the intent agent.
- Prompts in the house style, so complex questions (#01, #08) produce valid SPARQL and fewer queries return 0 rows.
- Cut tokens: about 3,000 per request today.
- Measure the intent agent's false alarms and its resistance to prompt injection.

**C · Data and context**

- Build the terminology index and add English names for ICD-10-GM and CHOP codes.
- Better example retrieval, e.g. embeddings.
- Write and validate more SPHN examples in the SIB `sparql-examples` format.

**D · Guard and judge**

- Encode the gray zones once the organizers answer.
- Add judge style rules: inference on code patterns, `COUNT(DISTINCT ?g)`, pattern order.
- Prevent Virtuoso-only errors without querying the database.
- Add performance rewrites.
- Add more trap tests.

**E · Eval and demo**

- Grow to 80+ labeled questions: paraphrases, prompt-injection attempts, gray zones.
- Run eval questions in parallel.
- Store reference results to check correctness.
- Build a 10-run timing harness on perf.
- Polish the demo and prepare the numbers for the report and video.

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
| Santiago Figueroa Luchetti | Architecture and backend | Architecture, B (agents and models), run machine |
| Adrika | Backend | |
| Gayathri | Backend | |
| David | Backend | A (integration) |
| Odinaka | Frontend | |

Pick your module in [section 5](#5-repository-map-and-module-owners) and write it here and in the owner column there.
