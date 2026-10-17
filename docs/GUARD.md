# Compliance guard: developer guide

For whoever works on module D (guard and judge). Read [DATA.md](DATA.md) first: the guard's decisions only make sense once you know what a patient graph looks like. Nothing in this guide needs a database or a GPU.

## Start here

| What | What it gives you |
|---|---|
| [DATA.md](DATA.md) | How the data looks without having the database: patient graphs, an annotated real patient, what each property holds, codes, query shapes, quirks |
| This guide | The guard inside: sensitivity levels, the five rules with examples, tools, how to change a rule, current assumptions, rdflib quirks, backlog |
| `data/guard_corpus.jsonl` | 37 LLM-generated queries with labels and recorded guard verdicts; see the module guide for the sanitized comparison with current local behavior |
| `data/examples.jsonl` | The 16 official examples, labeled |
| `scripts/check_query.py` | Give it a query: verdict, broken rules and what each variable can hold |
| `tests/test_guard.py` | The existing test cases; add yours there |

First task: backlog item 1 in [section 8](#8-backlog-for-module-d-most-useful-first).

## 1. What the guard decides

The guard receives the final SPARQL query and the intent agent's flags, and returns the verdict. It is code: about 15 ms per query, no tokens.

| Intent agent (request) | Query check (the five rules) | Verdict |
|---|---|---|
| OK | OK | compliant |
| flagged | broken | non-compliant, no retry |
| flagged | OK | non-compliant, no retry (the query agent cleaned up a forbidden request) |
| OK | broken | non-compliant, plus a retry hint for the query agent; still broken after the one retry means non-compliant |

Two promises never change:
- A query that breaks a rule is never marked compliant.
- A flagged request is never retried.

Both kinds of error cost points, so **false alarms matter as much as leaks**.

## 2. Code map

| File | Role |
|---|---|
| `src/nl2sparql/sparql/parse.py` | Removes Virtuoso extensions (`DEFINE`, `OPTION (inference …)`), parses with rdflib, and reports the query form: `SelectQuery`, `ConstructQuery`, `AskQuery`, `DescribeQuery`, `Update` or `Invalid` |
| `src/nl2sparql/guard/provenance.py` | Works out what each variable can hold: its **sensitivity** |
| `src/nl2sparql/guard/rules.py` | The five rules, R1–R5, as checks on the parsed query |
| `src/nl2sparql/guard/decision.py` | Combines rules and intent flags; writes `compliance_reason` and the retry hint |
| `src/nl2sparql/judge.py` | Shares the parser. Checks syntax and terms absent from the data, before the guard |

## 3. How a query is judged

### Step 1: what can each variable hold?

`analyse()` walks the rdflib algebra and gives every variable a sensitivity level. When a variable gets several levels, the highest wins.

| Level | Meaning | Comes from |
|---|---|---|
| `CONSTANT` | Literals and VALUES rows | `VALUES`, `BIND` of constants, `IF` with constant branches |
| `CATEGORY` | Classes, code concepts, value sets, labels | object of `rdf:type`, value-set properties, anything inside the ontology graph |
| `VALUE` | Measured values and numbers derived from data | literal properties, `COUNT`/`SUM`/`AVG`, `YEAR(...)`, `bif:datediff`, arithmetic |
| `TIMESTAMP` | Exact dates and times | dateTime properties |
| `INSTANCE` | Record and code-node IRIs (unique per patient) | the subject of any patient-graph pattern, link properties |
| `IDENTIFIER` | Patient graph names, identifier literals | `GRAPH ?g`, `hasIdentifier`, `hasSharedIdentifier`, names containing `Identifier` |

Propagation:
- **`BIND`:** functions that keep identity (`STR`, `CONCAT`, `REPLACE`, `STRAFTER`, `COALESCE`, the hashes, …) keep the input's level, so `STR(?g)` is still an `IDENTIFIER`. Anything else (numbers, booleans, `YEAR`) drops to `VALUE`. `IF` takes the level of its branches only.
- **Aggregates:** `COUNT`, `SUM` and `AVG` give `VALUE`. `MIN`, `MAX`, `SAMPLE` and `GROUP_CONCAT` keep the level of what they pick.
- **Classes stay classes:** the object of `rdf:type` is capped at `CATEGORY`, even if the query also uses it as a subject.

### Step 2: the rules

| Rule | Check in `rules.py` | Fails on |
|---|---|---|
| R4 | Query form must be `SelectQuery`, with no federated `SERVICE` clause | CONSTRUCT, ASK, DESCRIBE, any update, or SERVICE access to another endpoint |
| R1 | No patient graph IRI (`http://example.org/graph/…`) or record IRI (`…/sphn-resource/…-sphn-…`) anywhere, including `FROM` and `FROM NAMED`; no identifier literal in a pattern; no filter comparing an identifier with a constant | `GRAPH <…/mock_data-1_0042>`, `FROM NAMED <…/mock_data-1_0042>`, `?s sphn:hasIdentifier "ID-1…"`, `FILTER(STR(?g) = "…")` |
| R3 | At least one patient pattern with a concrete property, or `rdf:type` with a concrete class | `?s ?p ?o` alone, `?s a ?class` alone |
| R2 | The outermost SELECT is aggregated. No `SELECT *`. Every projected value is an aggregate or a `GROUP BY` key that is not instance-level or an exact timestamp; `HAVING (COUNT(DISTINCT ?g) = 1)` is rejected | rows of values, `GROUP BY ?date`, `GROUP_CONCAT` of values, `SAMPLE` of a timestamp, one-patient groups |
| R5 | No projected value is an identifier or a record IRI, even through `GROUP_CONCAT`, `SAMPLE`, `MIN`, `MAX` or `STR` | `GROUP_CONCAT(?pseudo_id)`, `MAX(?id)`, `GROUP BY ?g` with `?g` returned |

**Exception:** queries that touch only the ontology graph skip R2 and R5.

## 4. Tools

```bash
python -m pytest tests/test_guard.py -q          # the guard's tests (official examples + trap cases)
python scripts/check_query.py my_query.rq        # verdict, broken rules and every variable's level
pbpaste | python scripts/check_query.py -        # same, from the clipboard
```

**The corpus**, `data/guard_corpus.jsonl`, holds 37 queries the LLM really wrote. Each line has:
- `question_id`, `question`, `expected_compliant` (the label of the question);
- `sparql`;
- `intent_flags`;
- `pipeline_compliant`;
- `guard_violations_now`.

Two filters tell you where to look:
- `expected_compliant` true and `guard_violations_now` not empty: candidate false alarms that need review. Current local inspection finds parse errors on #01 and #08, so these are invalid SPARQL cases; #07's committed non-compliant status differs from current local Guard/Judge acceptance. See `docs/guard-and-judge/README.md` for the sanitized status. Fresh LLM and retry logs are needed to resolve the historical discrepancy.
- `expected_compliant` false and no violations: the intent agent must catch it alone. Today: none, because the LLM translated every forbidden request literally.

Regenerate the corpus on the run machine after new LLM runs: `python scripts/export_guard_corpus.py`.

## 5. Changing or adding a rule

1. Write the failing case first: add it to `CRAFTED` or `GROUPING_CASES` in `tests/test_guard.py`, with the expected verdict and rule.
2. Change `provenance.py` (what a variable holds) or `rules.py` (what is allowed).
3. Run `python -m pytest -q`. Every official example must keep its label.
4. Run `check_query.py` on the corpus cases you expect to change.
5. Push a branch `guard/<topic>` and ask Santiago to run `scripts/runbox.sh eval --execute --branch guard/<topic>`. Verdicts must not get worse, and no forbidden request may become compliant.

## 6. Current assumptions (gray zones)

These are our defaults until the organizers answer (README, section 14):

| Case | Today | Where |
|---|---|---|
| ASK / DESCRIBE | Rejected: only SELECT | R4 |
| `GROUP BY` on an exact timestamp | Rejected | R2 |
| `GROUP BY YEAR(?date)` | Allowed | R2 |
| `MIN`/`MAX` of dates or values | Allowed, even if it points at one patient | R5 only checks identifiers |
| `SAMPLE` of a value | Allowed; of a timestamp, rejected | R2 |
| Minimum group size | `HAVING (COUNT(DISTINCT ?g) = 1)` is rejected; larger thresholds are not enforced | R2 |
| Ontology-only queries | Allowed | exception above |
| `hasSubjectPseudoIdentifier` | Treated as an identifier (its name contains `Identifier`) | provenance |
| `FILTER(STRSTARTS(STR(?g), "…"))` | Allowed for the bare prefix, rejected when the literal contains digits | R1 |

## 7. rdflib quirks

The parser library has quirks we hit and handled:
- Aggregate nodes are named `Aggregate_Count`, `Aggregate_Max`, … with no trailing underscore.
- Projected `GROUP BY` keys become internal `SAMPLE(...)` aggregates; we skip those when the key is checked.
- `GROUP BY (YEAR(?d))` without `AS` loses the expression (`[None]`). We judge the projection by what reaches the output instead.
- Aggregate result names (`__agg_1__`, …) restart in every subquery. Each `AggregateJoin` overwrites them, which only works because the traversal is post-order.
- `pyparsing` must stay at 3.2.0. Newer versions flood the logs with warnings.

## 8. Backlog for module D (most useful first)

1. **Reconcile corpus status after a fresh run-machine LLM evaluation.** Locally, #01 and #08 have parse errors and #07 is accepted by current Guard/Judge, unlike its committed status. Review sanitized post-retry outcomes and logs; do not infer the cause from the corpus alone.
2. **Cut retries.** 32% of LLM requests use the retry. The judge now flags missing inference on explicit ICD-10-GM/CHOP code patterns and non-distinct `COUNT(?g)` patient counts (algebra-based, so a `COUNT(?g)` over a `SELECT DISTINCT ?g` subquery passes); missing standard prefixes are repaired automatically. Find which judge errors still trigger most retries (`logs/requests.jsonl` on the run machine).
3. **Catch Virtuoso-only errors statically.** LLM queries pass our parser but fail in Virtuoso, for example with `NULL`, `MEDIAN`, `OVER`, a declared `bif:` prefix or `xsd:year()`. We may not ask the database at runtime. The patterns found by the query eval are listed in [SPARQL_LLM_VALIDATOR.md](SPARQL_LLM_VALIDATOR.md).
4. **More trap tests:**
   - identifiers smuggled through `CONCAT`, `SUBSTR` or `STR(?record)`;
   - a subquery that projects `?g`, wrapped in an outer aggregate (safe when only a count is returned; unsafe when concatenated or sampled);
   - less common combinations of nested graph scope, filters, binds, aggregates, grouping and property paths. Existing tests cover identity transforms, nested patient projections, `FROM NAMED`, `OPTIONAL`/`UNION`, inverse/alternative paths, and a combined nested graph/path/filter/bind/aggregate case.
5. **`SERVICE` and singleton groups.** `SERVICE` (federated) clauses are rejected as R4 in the current code; confirm with the organizers that this is wanted. Ask whether singleton groups such as `HAVING (COUNT(DISTINCT ?g) = 1)` need special handling before changing behavior.
6. **Encode the gray zones** once the organizers answer: minimum group size, fine-grained grouping, and `MIN`/`MAX`.
6. **Later, with module B:** performance rewrites for the slow shapes (README, section 11).
