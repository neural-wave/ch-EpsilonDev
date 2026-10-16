# Base Pipeline Implementation Plan

> **For agentic workers:** this plan is executed inline. Each task ends with its tests passing and a commit. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every component of the design exists, runs end to end and has a clear interface, so each teammate can take one module and improve it independently.

**Architecture:** a FastAPI endpoint calls an async `Pipeline`. The pipeline runs the intent agent in parallel with context → query agent → judge, then the compliance guard decides, with one shared retry. LLM agents have two interchangeable backends: a deterministic baseline (default, no GPU) and an OpenAI-compatible client for the hackathon GPU. Spec: `docs/specs/2026-10-16-nl-to-sparql-design.md`.

**Tech stack:** Python 3.11+, FastAPI, httpx, rdflib (SPARQL parsing), pytest. Virtuoso 7.2.16 (local dev :8890, perf :8891) for data profiling, the demo and evaluation only.

**Scope:** base versions only. Quality work (prompts, retrieval, code linking, optimization) comes after, per module.

---

## Module map and owners

| Module | Path | Base version | Owner |
|---|---|---|---|
| Config and shared types | `src/nl2sparql/config.py`, `models.py` | env settings, dataclasses | integration |
| Input check | `input_check.py` | normalize, reject empty/too long | integration |
| SPARQL parsing | `sparql/` | strip Virtuoso extensions, rdflib parse, prefix fixer, IRI collector | guard owner |
| Compliance guard | `guard/` | 5 rules on the query + 4-case decision with intent flags | guard owner |
| Judge | `judge.py` | syntax + unknown SPHN terms + missing prefixes | guard owner |
| Context | `context/` | BM25 examples, schema cards, terminology lookup | data owner |
| Agents | `agents/`, `llm.py` | heuristic intent, example-retrieval query, LLM versions | agents owner |
| Orchestrator | `orchestrator.py`, `factory.py` | parallel intent, shared retry, timings, tokens, log | integration |
| API and demo | `api.py`, `__main__.py`, `demo/`, `virtuoso.py` | `/nl-to-sparql`, `/health`, demo page | integration |
| Eval | `eval/` | questions + runner + report | eval owner |
| Data scripts | `scripts/` | import examples, profile data, terminology index | data owner |
| Infra | `infra/virtuoso/` | dev and perf wrappers around the release scripts | data owner |

## Interfaces every module relies on

```python
# models.py
IntentFlags(asks_for_identifiers, single_patient, individual_rows, whole_graph, not_a_select,
            reason: str | None, suggested_rephrase: str | None)   # .flagged -> bool
Example(id, question, sparql, compliant, source)
Context(examples: list[Example], schema_cards: list[str], code_candidates: list[CodeEntry])
JudgeResult(sparql: str, errors: list[str])                        # errors == [] means OK
PipelineResult(sparql_query, tokens_consumed, is_compliant, compliance_reason,
               suggested_rephrase, intent, violations, retries, timings_ms)

# agents/base.py
class IntentAgent(Protocol):
    async def classify(self, text: str) -> tuple[IntentFlags, int]          # (flags, tokens)
class QueryAgent(Protocol):
    async def generate(self, text: str, context: Context, feedback: str | None) -> tuple[str, int]

# guard
ComplianceGuard.decide(sparql: str, flags: IntentFlags) -> GuardDecision(compliant, reason, retry_hint, violations)
# judge
Judge.check(sparql: str) -> JudgeResult
# context
ContextBuilder.build(text: str) -> Context
# orchestrator
await Pipeline.run(text: str) -> PipelineResult        # raises InputError, PipelineError
```

## Tasks

- [x] **1. Skeleton:** `pyproject.toml`, `requirements.txt`, `.gitignore`, `.env.example`, `config.py`, `models.py`. Check: `pip install -r requirements.txt` and `python -c "import nl2sparql"`.
- [x] **2. Input check** + `tests/test_input_check.py`.
- [x] **3. SPARQL parsing** (`sparql/parse.py`, `prefixes.py`, `terms.py`) + `tests/test_sparql.py`.
- [x] **4. Data assets:** `scripts/import_examples.py` and `scripts/profile_data.py` write `data/examples.jsonl`, `data/property_kinds.json`, `data/schema_cards.json` and seed `eval/questions.jsonl`.
- [x] **5. Guard** (`guard/provenance.py`, `rules.py`, `decision.py`) + `tests/test_guard.py`: the 15 official examples and crafted cases must match their labels.
- [x] **6. Context** (`context/bm25.py`, `examples.py`, `schema.py`, `terminology.py`, `builder.py`) + `scripts/build_terminology_index.py` + `tests/test_context.py`.
- [x] **7. Judge** + `tests/test_judge.py`.
- [x] **8. LLM client and agents** (`llm.py`, `agents/*`) + `tests/test_agents.py` (LLM calls mocked with `httpx.MockTransport`).
- [x] **9. Orchestrator and factory** + `tests/test_orchestrator.py` (fake agents: parallelism, retry rules, tokens, errors).
- [x] **10. API, demo and Virtuoso client** + `tests/test_api.py` (contract from `API_specification.md`).
- [x] **11. Eval runner** (`eval/run_eval.py`) and first baseline report.
- [x] **12. Infra wrappers and README.**
- [x] **13. Final check:** full test suite, API smoke test with curl, eval run, commit.
