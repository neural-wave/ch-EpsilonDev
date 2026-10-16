# NL-to-SPARQL Gateway: Design Spec

- **Date:** 2026-10-16
- **Status:** direction approved by the team; some details wait on answers from the organizers (section 9).
- **Challenge:** SIB / SPHN, "Secure NL-to-SPARQL over Clinical Knowledge Graphs", Neural Wave hackathon.
- **Deadline:** Sunday 2026-10-18, 11:00.

## 1. Problem

A researcher asks a question in English. The system must:

1. translate it into a SPARQL query over SPHN patient data stored in Virtuoso, and
2. decide whether that query complies with the privacy rules.

Every request goes through one endpoint, `POST /nl-to-sparql`, with body `{"text": "..."}`. The response always contains `sparql_query`, `tokens_consumed` and `is_compliant`, plus `compliance_reason` when `is_compliant` is false. A non-compliant request is still a successful `200` response.

### Privacy rules (Query_Compliance.md)

- R1. No query about a single, identifiable patient.
- R2. Results are always aggregated across all patients.
- R3. No matching of the whole graph without restriction.
- R4. Only `SELECT` queries.
- R5. No identifiers exposed (patient, lab, diagnosis IDs, ...).

### How we are scored

| Criterion | What is measured |
|---|---|
| 1. Efficiency (main) | Total time per request: translation + compliance + any retry |
| 2. Compliance correctness (main) | Our `is_compliant` against the jury's label. False positives and false negatives are both penalized |
| 3. Query performance (main) | Execution time of our query on the jury's machine, average of 10 isolated runs |
| Minor | Tokens per request, smaller models, RAM |

### Hard constraints

- Every tool and model has an open-source license. Apertus is encouraged. The organizers provide GPUs.
- The environment installs with `pip install -r requirements.txt` and works out of the box.
- The repo becomes public after the event. Restricted terminologies (SNOMED CT, ICD-10-GM, CHOP, LOINC, UCUM), anything derived from them, and the patient data must never be committed.

## 2. Design drivers

1. **One LLM call on the critical path** in the normal case. The intent check runs in parallel with generation, not before it.
2. **Code wherever possible.** Input check, context retrieval, judge and compliance guard are deterministic code: milliseconds, zero tokens, same answer every time.
3. **Literal translation.** The query agent never drops or softens part of a request. A forbidden request yields its literal query, marked non-compliant, and that query never runs.
4. **Verdict on both the request and the query.** The jury labels requests; the privacy risk lives in the query. The guard combines both.
5. **Queries in the house style** of the official examples, measured on the 100k-patient database.

## 3. Architecture

```
POST /nl-to-sparql {"text"}
        │
        ▼
 Input check (code) ──── empty / too long ──► HTTP 400 {"error"}
        │
        ├──────────────────────────────┐   (in parallel)
        ▼                              ▼
 Intent agent (small LLM)       Context (code)
 5 flags + reason               examples · schema cards · code candidates
 + suggested_rephrase                  │
        │                              ▼
        │                       Query agent (LLM) ◄─────────────┐
        │                              │ SPARQL                  │ 1 retry
        │                              ▼                         │ (shared)
        │                       Judge (code) ── syntax/schema ───┤
        │                              │ final SPARQL            │
        ▼                              ▼                         │
 Compliance guard (code): 5 rules on the query + intent flags ───┘
        │                                  (allowed request, broken rule)
        ▼
 HTTP 200 {sparql_query, tokens_consumed, is_compliant, compliance_reason?}
        ┆  outside the API (demo UI only)
        ├─ compliant     → run on Virtuoso, show the aggregate
        └─ non-compliant → nothing runs, show reason + "Ask this instead"
```

A detailed version of this flow, traced for one allowed and one forbidden request, lives on the team diagram page.

## 4. Components

### 4.1 API layer

- FastAPI app exposing `POST /nl-to-sparql`, exactly as in `API_specification.md`. No extra response fields.
- Forbidden requests: the API returns the literal query because the jury asked for it, to judge better. A real user gets only the reason: `RETURN_NONCOMPLIANT_QUERY=false` switches to that behavior (empty `sparql_query`).
- Database-agnostic: nothing behind the endpoint connects to a database (jury feedback, 2026-10-16).
- Status codes: `200` for every finished translation; `400` for invalid input; `5xx` when the pipeline cannot produce a query (for example, the LLM server is down). Errors use `{"error": "..."}`.
- `tokens_consumed` is the sum of prompt and completion tokens reported by the LLM server for every call in the request (intent, generation, retry).
- Timeouts: one per LLM call and one for the whole request, both configurable.

### 4.2 Input check (code)

- Rejects only empty or whitespace-only text and text above a length limit (default 2,000 characters).
- Normalizes: Unicode NFC, control characters removed, whitespace collapsed, emoji removed.
- Never rejects a request because of its characters. Clinical text contains `≥`, `µ`, `°` and accented names.

### 4.3 Intent agent (small LLM)

- **Input:** the normalized request text only.
- **Output:** JSON, constrained by a schema at decoding time when the server supports it:

```json
{
  "asks_for_identifiers": false,
  "single_patient": false,
  "individual_rows": false,
  "whole_graph": false,
  "not_a_select": false,
  "reason": null,
  "suggested_rephrase": null
}
```

- `reason` and `suggested_rephrase` are filled only when at least one flag is true.
- Prompt: the five rules phrased as questions, plus few-shot requests labeled allowed or forbidden (the 15 official ones plus our own). Temperature 0.
- Runs concurrently with the context step and the query agent. It never blocks generation.

### 4.4 Context (code)

Built offline by a script from the local Virtuoso and the ontology files. The built assets are git-ignored because they derive from restricted terminologies; the script rebuilds them on any machine that has the release files.

- **Schema cards.** One card per SPHN class present in the data: the properties actually used, their target classes or datatypes, and how many patients have the class. The source is a profile of the data, cross-checked with the SHACL shapes. The data wins over the 2026.1 schema file (for example, the data uses `sphn:hasRank`, the schema says `sphn:hasRankCode`).
- **Example bank.** Natural-language question plus SPARQL pairs: the 15 official examples, the public SPHN SPARQL examples that return results on our dev database, and our own.
- **Terminology index.** Labels (German, English, French) for ICD-10-GM, CHOP, SNOMED CT, LOINC and ATC, mapped to their class IRIs.

At request time, within a budget of about 50 ms:

- the top-k similar examples, by embedding similarity with a small multilingual model on CPU or by BM25;
- the schema cards of the classes the question touches;
- code candidates for the medical terms in the question, by fuzzy match against the terminology index.

### 4.5 Query agent (LLM)

- **Input:** the request and the context. **Output:** one SPARQL query and nothing else.
- Instructions:
  - Translate literally. Keep every part of the request, including parts that break a rule.
  - Use only classes and properties from the schema cards, and only codes from the candidates or the index.
  - Patient = named graph `?g`. Count patients with `COUNT(DISTINCT ?g)`, usually in a `SELECT DISTINCT ?g` subquery.
  - Put `OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")` on code type patterns only (`?code a icd-10-gm:E11`). Codes in the data are versioned (`icd-10-gm/2022/A00.1`), so matching without inference finds nothing.
  - Write the most selective pattern first and add `DEFINE sql:select-option "order"` when that order is clear.

### 4.6 Judge (code)

- **Parse.** Remove the Virtuoso extensions (`DEFINE` lines, `OPTION (inference ...)` clauses), then parse with rdflib's SPARQL parser. A syntax error triggers the retry.
- **Schema check.** Every `sphn:` class and property must exist in the schema cards. Every terminology IRI must exist in the index. An unknown term triggers the retry, with the closest valid names in the feedback.
- **Style fixes** applied in code, without a retry: missing `PREFIX` declarations, the inference option on code type patterns.
- **No database access:** the judge never contacts a database. The jury ruled out the optional `EXPLAIN` check: the system must be database-agnostic.

### 4.7 Compliance guard (code)

Works on the parsed query, not on its text.

| Rule | Check |
|---|---|
| R4 | The query form is `SELECT`. No update operation (`INSERT`, `DELETE`, `LOAD`, `CLEAR`, `DROP`, `CREATE`) |
| R1 | No constant patient graph IRI in `GRAPH`, `VALUES` or a filter on `?g`. No filter on an identifier literal |
| R2 | No `SELECT *`. Every variable projected by the outermost query is an aggregate or a `GROUP BY` key that is not instance-level |
| R3 | At least one triple pattern restricts by a concrete class, property or code. A lone `?s ?p ?o` fails |
| R5 | No projected value derives from `sphn:hasIdentifier`, `sphn:hasSharedIdentifier`, `?g` or a record IRI, including inside `GROUP_CONCAT`, `SAMPLE`, `MIN` and `MAX` |

A variable is instance-level when it is the subject of a record pattern (`?dx a sphn:BilledDiagnosis`) or holds a record IRI. Identifier literals and exact timestamps also count as instance-level (see section 9). Category-level values (a code class from `?code a ?concept`, a `sphn-individual:` value, a computed label) are fine as group keys.

**Combining with the intent flags:**

| Intent agent | Query check | Meaning | Verdict |
|---|---|---|---|
| OK | OK | Normal case | compliant |
| flagged | broken | Both agree the request is forbidden | non-compliant, no retry |
| flagged | OK | The query agent cleaned up a forbidden request | non-compliant, no retry |
| OK | broken | The query agent made a mistake on an allowed request | 1 retry naming the broken rule; still broken means non-compliant |

**Fixed rules:**
- A query that breaks a rule is never marked compliant.
- A forbidden request is never retried, because a retry could turn it into a clean query.

**`compliance_reason`** is built as follows:
1. A fixed template for the broken rule, filled with the offending part of the query.
2. The intent agent's `reason`, when present.
3. A rephrase suggestion: the intent agent's `suggested_rephrase`, or a fixed hint per rule when the intent agent did not flag the request.

### 4.8 Orchestrator

- `asyncio`: input check, then the intent agent runs concurrently with context → query agent → judge, then the guard.
- One retry per request, shared between the judge and the guard.
- No early exit: the jury wants the literal query of forbidden requests in the response, so generation always runs.
- Every request is logged as one JSONL line: text, flags, query, verdict, per-step timings, tokens, retries.

### 4.9 LLM serving

- One OpenAI-compatible client, configured by `LLM_BASE_URL` and the model names for the intent and query agents.
- **Development:** a small local model on a laptop (Ollama or MLX), so nobody waits for the GPU.
- **Benchmarking and judging:** vLLM on the organizers' GPU, with JSON-schema-constrained decoding for the intent agent.
- **Candidates:** Apertus 8B Instruct and an Apache-2.0 instruct or coder model in the 7-14B range. The intent agent may use a smaller model than the query agent. We pick the smallest model that meets the test bench targets.
- Llama-family models are excluded: their license is not open source in the strict sense.

### 4.10 Demo UI (for the video only)

A single page:
1. A question box that calls the API.
2. Shows the verdict. For a compliant request it also shows the SPARQL.
3. Optionally runs a compliant query on the local Virtuoso to check the result and its execution time. The jury confirmed this is not required.
4. For a forbidden request it shows only the reason and the suggestion, never the query, plus an "Ask this instead" button. The button sends the suggestion as a new request through the full pipeline.

The demo UI is not part of the judged API.

## 5. Evaluation harness

- **Test set:** `eval/questions.jsonl`, one object per line: `id`, `question`, `expected_compliant`, `category`, optional `reference_sparql`, `notes`.
- **Sources:**
  - the 15 official examples;
  - 60-80 requests written by the team, covering simple and complex allowed requests, at least five violations per rule, paraphrased violations, "sanitization traps" (allowed question plus a forbidden tail), and gray zones.
- **Metrics:**
  - verdict accuracy, false positives and false negatives;
  - latency p50 and p95, tokens per request, retry rate;
  - for allowed requests: result match against `reference_sparql` on the dev database, and execution time on the perf database (10 runs for finalists).
- The full run takes under 10 minutes, so we can run it after every change.

## 6. Local environment (already running)

| | Dev | Perf |
|---|---|---|
| Patients | 2,000 | 100,000 |
| Triples (patients + ontologies) | 10.8M + 13.0M | 611.7M + 13.0M |
| SPARQL endpoint | `http://localhost:8890/sparql` | `http://localhost:8891/sparql` |

These databases are for development only: profiling, checking results and measuring performance. The pipeline never connects to them (jury feedback, 2026-10-16).

Example #05 takes 25.6 s on dev. On perf, #01 exceeds 30 minutes and #03 takes 28 minutes, so query performance is a major lever.

## 7. Repo layout

```
nl2sparql/                 # becomes NW_26_<Company>_<Team>
  README.md                # overview, run instructions, members and roles
  requirements.txt
  src/nl2sparql/
    api.py  orchestrator.py  input_check.py  intent.py
    context/  query_agent.py  judge.py  guard/  llm.py  config.py
  scripts/                 # build_assets.py, profile_data.py, validate_examples.py
  eval/                    # questions.jsonl, run_eval.py, reports/
  demo/
  tests/
  docs/specs/  docs/report/
```

`.gitignore` covers built assets, ontology files, patient data, logs and `.env`.

## 8. Out of scope

- Multi-turn conversation and user accounts.
- Result-level privacy, such as suppressing small counts. This goes in the report as future work.
- Federated execution across hospitals.

## 9. Assumptions pending organizer answers

| Question | Default until answered |
|---|---|
| Only `SELECT` allowed? (API spec and rules disagree) | Yes, only `SELECT` |
| Forbidden request: literal query expected? | **Answered by the jury:** return it so they can judge; a real user gets only the reason |
| Is result correctness evaluated? | Yes, we optimize for correct results |
| How is our system run, on which hardware, and how do we reach the GPU? | Model behind an OpenAI-compatible URL set by environment variable |
| Can the system use a database at runtime? | **Answered by the jury:** no, it must be database-agnostic |
| Must compliant queries be executed? | **Answered by the jury:** no, it is optional |
| How are queries timed: HTTP or `query.sh` (isql)? | HTTP. If isql, avoid constructs isql rejects, such as `;` inside `GROUP_CONCAT` |
| Gray zones: fine-grained `GROUP BY`, `MIN`/`MAX` that reveal one patient | `GROUP BY` on identifiers or exact timestamps is non-compliant; `MIN`/`MAX` over dates is allowed |
| Request language | English only |
