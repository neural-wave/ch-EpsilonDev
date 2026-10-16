# Guide for AI coding assistants

Read [README.md](README.md) first. It is the team's master document: the challenge, the jury's feedback, the architecture and its reasons, setup, configuration, the GPU, the run machine, baseline numbers, known pitfalls and the backlog per module. This file only repeats what you must never get wrong.

## Rules you must follow

- **Never commit or paste restricted material.** That covers ontology and terminology files (SNOMED CT, ICD-10-GM, CHOP, LOINC, UCUM), anything derived from them (`assets/`), patient data, `.env`, tokens and GPU host details. The repo becomes public after the event.
- **Keep the pipeline database-agnostic.** Nothing reachable from `POST /nl-to-sparql` may query Virtuoso or any other database: the jury requires it. Virtuoso is only for scripts, `eval --execute` and the demo's optional dev check.
- **The API contract is fixed** (README §1). Field names must not change, and a forbidden request still answers HTTP 200 with `is_compliant: false` and a `compliance_reason`.
- **Translate literally.** Never let the query agent drop or soften the forbidden part of a request. A query that breaks a rule is never marked compliant, and a request the intent agent flags is never retried.
- **Only open-source models** in the shipped system.
- **Shared interfaces need the team's agreement:** `src/nl2sparql/models.py` and `src/nl2sparql/agents/base.py`.
- **Code, comments and documents in English.** Match the style of the surrounding code.

## How to work

- Stay inside the module you were asked to improve (README §5 maps modules A–E to folders).
- Every bug fix gets a regression test next to the existing ones in `tests/`.
- Before you finish, run `python -m pytest -q`. Everything must pass; no Virtuoso or GPU is needed.
- For behavior changes, the team runs `python eval/run_eval.py` on the run machine with the LLM. Report the summary and compare it with the baseline in README §11.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env && set -a && source .env && set +a
python -m pytest -q
python -m nl2sparql --port 8080          # API + demo
python eval/run_eval.py                  # verdict accuracy, latency, tokens
```

## Domain facts that trip up assistants

- **One patient is one named graph.** Count patients with `COUNT(DISTINCT ?g)`. Returning `?g` exposes the patient.
- **Codes are typed nodes and carry a year:** `?x sphn:hasCode ?c . ?c a icd-10-gm:E11 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")`. Without the inference option nothing matches.
- **The data, not the 2026.1 schema, is the truth.** For example `sphn:hasRank` exists in the data and `sphn:hasRankCode` does not. Check `data/schema_cards.json`.
- **Qwen3.x needs `LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'`**, or it spends its whole token budget thinking.
- **rdflib quirks affect the guard:**
  - Aggregate node names have no trailing underscore.
  - Projected `GROUP BY` keys become `SAMPLE`.
  - `GROUP BY (expr)` without `AS` loses the expression.
  - `__agg_N__` names repeat across subqueries.
- **Do not act on the GPU instance unless asked.** If asked, read `/etc/vast-agents-guide.md` on it first.
