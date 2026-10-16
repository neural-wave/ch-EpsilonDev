# NL-to-SPARQL Gateway

Translates a researcher's question in English into a SPARQL query over SPHN clinical data, and decides whether that query respects the privacy rules. Built for the SIB / SPHN challenge at the Neural Wave hackathon 2026.

```
POST /nl-to-sparql {"text"}
  → Input check (code)
  → in parallel:  Intent agent (LLM)   |   Context (code) → Query agent (LLM) → Judge (code)
  → Compliance guard (code): 5 rules on the final query + intent flags   (1 retry, shared)
  → {sparql_query, tokens_consumed, is_compliant, compliance_reason}
```

The API never executes queries. Only the demo page and the eval harness run compliant queries on Virtuoso. Design: [docs/specs/2026-10-16-nl-to-sparql-design.md](docs/specs/2026-10-16-nl-to-sparql-design.md).

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a && source .env && set +a
python -m nl2sparql --port 8080
```

```bash
curl -s localhost:8080/nl-to-sparql -H 'Content-Type: application/json' \
  -d '{"text": "How many patients have a billed diagnosis of cholera, by sex?"}'
```

The demo page is at <http://localhost:8080/>. It needs a running Virtuoso (see [infra/virtuoso](infra/virtuoso/README.md)).

## Backends

`NL2SPARQL_BACKEND=stub` (the default) runs without any model: a keyword intent agent and a query agent that returns the most similar example. It exists so the whole pipeline runs before the GPU is ready.

To use a real model, point the pipeline at any OpenAI-compatible server, such as vLLM on the hackathon GPU:

```bash
NL2SPARQL_BACKEND=llm
LLM_BASE_URL=http://<gpu-host>:8000/v1
LLM_QUERY_MODEL=<model served by vLLM>
LLM_INTENT_MODEL=<smaller model, or leave empty to reuse the query model>
```

## Data files

| File | What | Committed |
|---|---|---|
| `data/examples.jsonl` | 15 official examples + the worked example from the brief | yes |
| `data/property_kinds.json` | what each property points to (value, value set, link), measured on the data | yes |
| `data/schema_cards.json` | classes present in the data with their properties | yes |
| `assets/terminology_index.jsonl` | code IRIs with labels, from restricted terminologies | **no** |

Rebuild them with a running dev Virtuoso:

```bash
python scripts/import_examples.py --release-dir ../hackathon-virtuoso-release
python scripts/profile_data.py --endpoint http://localhost:8890/sparql
python scripts/build_terminology_index.py --endpoint http://localhost:8890/sparql
```

## Evaluation and tests

```bash
python eval/run_eval.py            # verdict accuracy, latency and tokens on all questions
python eval/run_eval.py --execute  # also runs compliant queries on VIRTUOSO_ENDPOINT
python -m pytest -q
```

Add test questions to `eval/questions.jsonl` with their expected label.

## Who owns what

Each module has one owner and a fixed interface (see [docs/plans/2026-10-16-base-pipeline.md](docs/plans/2026-10-16-base-pipeline.md)). Change the interfaces only together.

| Module | Files | Owner | Next improvements |
|---|---|---|---|
| Guard and judge | `src/nl2sparql/guard/`, `judge.py`, `sparql/` | | gray zones once the organizers answer; more trap cases in tests |
| Agents and prompts | `src/nl2sparql/agents/`, `llm.py` | | prompts, few-shot selection, model choice on the GPU |
| Data and context | `src/nl2sparql/context/`, `scripts/`, `infra/` | | English labels for codes, better example retrieval, perf profiling |
| Eval and demo | `eval/`, `src/nl2sparql/demo/` | | 80+ labeled questions, perf timings, demo for the video |
| Integration | `orchestrator.py`, `factory.py`, `api.py` | | early exit, timeouts, deployment |

## Rules for this repo

- The repo becomes public after the event. Never commit ontology files, patient data, or anything derived from SNOMED CT, ICD-10-GM, CHOP, LOINC or UCUM (`.gitignore` blocks the usual paths).
- LLM-based coding assistants were used to build this project. The final report says how.

## Team

| Name | Role |
|---|---|
| | |
