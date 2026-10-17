# Frontend: what to build on

For whoever builds the user interface (module E, Odinaka). The interface lives **outside the judged API**: the jury calls `POST /nl-to-sparql` directly and never sees the page. The page matters for the demo video and for the "what the doctor gets" story in the report.

## Where it lives today

- `src/nl2sparql/demo/index.html`: one static file, served at `http://localhost:8080/` by the API process. Replacing that file is the zero-configuration path. If the new interface needs several files (a built bundle, images), ask module A to mount the folder as static files: a three-line change in `src/nl2sparql/api.py`.
- Run it locally with the stand-in agents, no model needed: `python -m nl2sparql --port 8080` (see README section 6). On the run machine the same page runs with the real model.

## The endpoint to call

`POST /demo/ask` with `{"text": "<the question>"}`. It runs the whole pipeline and, for a compliant query, also runs it on the development database so the page can show the answer. Response fields:

| Field | Type | Meaning |
|---|---|---|
| `is_compliant` | bool | The verdict |
| `sparql_query` | string or `null` | The query, **only when compliant**. For a forbidden request it is `null` on purpose |
| `compliance_reason` | string or `null` | Why the request was refused, in plain English |
| `suggested_rephrase` | string or `null` | A closer allowed question, when the intent agent found one |
| `tokens_consumed` | int | Tokens of every model call in the request |
| `timings_ms` | object | `intent`, `context`, `generate`, `guard`, `total`, and the retry steps when they ran |
| `tokens_by_step` | object | `intent`, `query`, `query_retry` |
| `diagnostics` | object | Retry reason, first-try query, cut-off answers: developer detail, not for the doctor |
| `executed` | bool | Whether the query ran on the development database |
| `columns`, `rows` | list | The result when it ran (at most 200 rows) |
| `execution_ms` | number | Time of that execution |
| `execution_error` | string | When the database rejected the query |

Errors: HTTP 400 `{"error": "..."}` for empty or oversized text, HTTP 503 when the model does not answer or the request passes 75 seconds.

Do not call `POST /nl-to-sparql` from the page: it returns the literal query of forbidden requests (the jury asked for it) and never executes anything.

## Rules the page must keep

1. **Never show the query of a forbidden request.** The jury was explicit: a real user gets only the reason. `/demo/ask` already withholds it; do not fetch it elsewhere.
2. **Forbidden request:** show `compliance_reason` and, when present, `suggested_rephrase` as "Ask this instead", ideally as a button that sends it.
3. **Compliant request:** show the result table (`columns`, `rows`) as the answer, and the query as secondary detail (a collapsible block). Show `execution_error` plainly when it happens.
4. **Show the cost:** `tokens_consumed` and `timings_ms.total` are the two main scoring criteria, so a small line "2.8 s · 1,950 tokens" is worth having.
5. **The browser never talks to Virtuoso or to the model server.** Everything goes through the API process.
6. **English interface, no restricted content.** No terminology labels (SNOMED CT, ICD-10-GM, CHOP) typed into the page; the repository becomes public.
7. **Keep it usable at phone width** and in light and dark themes; the video may be recorded on a laptop.

## Good questions for the demo

Allowed, with results on the development database:
- "How many patients have a billed diagnosis of cholera, by sex?"
- "How many of our patients are recorded as male, as female, and as neither (indeterminate)?"
- "What is the average length of a hospital stay in days?"

Forbidden, with a reason and a rephrase:
- "How many patients have cholera, and which ones are they?"
- "How many lab tests has patient 0042 had?"
- "Dump everything in the database."

More labeled questions are in `eval/questions.jsonl` and `eval/intent_questions.jsonl`.
