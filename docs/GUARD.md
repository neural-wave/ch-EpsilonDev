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
| `tests/test_guard_pinning.py` | R1 on expressions: the shapes that pin `?g` or an identifier, and the house-style shapes that must stay allowed |

First task: backlog item 1 in [section 8](#8-backlog-for-module-d-most-useful-first).

## 1. What the guard decides

The guard receives the final SPARQL query and the intent agent's flags, and returns the verdict. It is code: about 15 ms per query, no tokens.

| Intent agent (request) | Query check (the five rules) | Verdict |
|---|---|---|
| OK | OK | compliant |
| flagged | broken | non-compliant, no retry |
| flagged | OK | non-compliant, no retry (the query agent cleaned up a forbidden request) |
| OK | R1 broken | non-compliant, no retry (the query names a patient graph, a record or an identifier: that came from the request) |
| OK | R2–R5 broken | non-compliant, plus a retry hint for the query agent; still broken after the one retry means non-compliant |

Three promises never change:
- A query that breaks a rule is never marked compliant.
- A flagged request is never retried.
- An R1 violation is never retried (`REQUEST_LEVEL_RULES` in `decision.py`). The model cannot invent a graph name, a record IRI or identifier digits, so a retry would only let it drop the forbidden part and hand back a clean query for a forbidden request (gen-106 in [FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md)).

The retry hint repeats every violation message and ends with one sentence: for grouping violations, "Group by a category (a code concept via `?c a ?concept`, or a value-set property), not by the patient or the record; keep one row per category."; otherwise "Rewrite it so that it only returns results aggregated across all patients."

Both kinds of error cost points, so **false alarms matter as much as leaks**.

## 2. Code map

| File | Role |
|---|---|
| `src/nl2sparql/sparql/parse.py` | Removes Virtuoso extensions (`DEFINE`, `OPTION (inference …)`), parses with rdflib, and reports the query form: `SelectQuery`, `ConstructQuery`, `AskQuery`, `DescribeQuery`, `Update` or `Invalid` |
| `src/nl2sparql/guard/provenance.py` | Works out what each variable can hold: its **sensitivity** |
| `src/nl2sparql/guard/rules.py` | The five rules, R1–R5, as checks on the parsed query |
| `src/nl2sparql/guard/decision.py` | Combines rules and intent flags; writes `compliance_reason` and the retry hint; never retries R1 |
| `src/nl2sparql/judge.py` | Shares the parser. Repairs what needs no model, checks syntax and terms absent from the data, before the guard; given the question, also that the query restricts to the coded concepts the question names (`judge_checks/coverage.py`) |

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
| R4 | Query form must be `SelectQuery`, with no federated `SERVICE` clause | CONSTRUCT, ASK, DESCRIBE, any update, or any SERVICE clause |
| R1 | No patient graph IRI (`http://example.org/graph/…`) or record IRI (`…/sphn-resource/…-sphn-…`) anywhere, including `FROM` and `FROM NAMED`; no identifier literal in a pattern; no expression that pins a graph name or an identifier (`IDENTIFIER` level) to values that are not identifiers themselves: comparisons (`=`, `IN`, …), string matchers (`REGEX`, `CONTAINS`, `STRSTARTS`, `STRENDS`, `sameTerm`), custom functions (`bif:starts_with`), wherever they sit (`FILTER`, `BIND`, `ORDER BY`, `GROUP BY`, an aggregate argument); no `VALUES` on such a variable; no `BIND` of a fixed value into a `GRAPH` variable | `GRAPH <…/mock_data-1_0042>`, `FROM NAMED <…/mock_data-1_0042>`, `?s sphn:hasIdentifier "ID-1…"`, `FILTER(STR(?g) = "…")`, `FILTER(STRSTARTS(STR(?g), CONCAT(…)))`, `FILTER(?g = IRI("…"))`, `SUM(IF(STRSTARTS(STR(?g), "…1050"), 1, 0))`, `VALUES ?g { … }`, `BIND(IRI("…") AS ?g)` |
| R3 | At least one patient pattern with a concrete property, or `rdf:type` with a concrete class | `?s ?p ?o` alone, `?s a ?class` alone |
| R2 | The outermost SELECT is aggregated. No `SELECT *`. Every projected value is an aggregate or a `GROUP BY` key that is not instance-level or an exact timestamp; `HAVING (COUNT(DISTINCT ?g) = 1)` is rejected | rows of values, `GROUP BY ?date`, `GROUP_CONCAT` of values, `SAMPLE` of a timestamp, one-patient groups |
| R5 | No projected value is an identifier or a record IRI, even through `GROUP_CONCAT`, `SAMPLE`, `MIN`, `MAX` or `STR` | `GROUP_CONCAT(?pseudo_id)`, `MAX(?id)`, `GROUP BY ?g` with `?g` returned |

**Exception:** queries that touch only the ontology graph skip R2 and R5.

**What R1 still allows:** `?g != ?g2`, `BOUND(?g)`, `COALESCE(?a, ?b)` and `IF(BOUND(?e), ?g, 0)` (nothing fixed by the request), `HAVING (COUNT(DISTINCT ?g) >= 5)` (a count, not the name), and a string matcher whose literal is digit-free, such as the house-style `FILTER(STRSTARTS(STR(?g), "http://example.org/graph/"))` or `FILTER(STRSTARTS(STR(?concept), "http://snomed.info/id/"))`. The 12 bypass shapes and the 11 allowed shapes are the cases of `tests/test_guard_pinning.py`.

**Grouping messages say what to do instead.** A "Grouping by …" violation (`Violation.grouping` is true) follows `BIND` chains back to the bound variable and appends the fix: a code node → "Group by the code's class instead: `?x sphn:hasCode ?code . ?code a ?concept . GROUP BY ?concept`"; a record linked by a property → "Group by the class of its code instead: `?rec sphn:hasCode ?c . ?c a ?concept`"; `?g` or an alias of it → "Never group by or return the patient graph; count patients with `COUNT(DISTINCT ?g)` and group by a category"; an exact timestamp → "Group by `YEAR(?start)` or `MONTH(?start)` instead". The real first tries behind it (intent-028, q-057, q-018, q-030) are `GROUPING_HINT_CASES` in `tests/test_guard.py`.

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

These describe the current implementation. Policy answers are still pending (README, section 14); confirm them before extending or relaxing these rules:

| Case | Today | Where |
|---|---|---|
| ASK / DESCRIBE | Rejected: only SELECT | R4 |
| `GROUP BY` on an exact timestamp | Rejected | R2 |
| `GROUP BY YEAR(?date)` | Allowed | R2 |
| `MIN`/`MAX` of dates or values | Allowed, even if it points at one patient | R5 only checks identifiers |
| `SAMPLE` of a value | Allowed; of a timestamp, rejected | R2 |
| Minimum group size | `HAVING (COUNT(DISTINCT ?g) = 1)` is rejected; larger thresholds are not enforced | R2 |
| Federated `SERVICE` | Rejected as R4 | R4 |
| Ontology-only queries | Allowed | exception above |
| `hasSubjectPseudoIdentifier` | Treated as an identifier (its name contains `Identifier`) | provenance |
| `FILTER(STRSTARTS(STR(?g), "…"))` | Allowed for a digit-free literal (the bare prefix), also in `REGEX`, `CONTAINS` and custom functions; rejected when the literal contains digits or is built from an expression or a variable | R1 |
| A second graph variable, `GRAPH ?death_patient { … }` | A patient graph like `?g`: projecting, grouping by or pinning it breaks the same rules; counting it is fine | provenance |
| An R1 violation on an allowed request | Never retried; the verdict stands | decision |

## 7. rdflib quirks

The parser library has quirks we hit and handled:
- Aggregate nodes are named `Aggregate_Count`, `Aggregate_Max`, … with no trailing underscore.
- Projected `GROUP BY` keys become internal `SAMPLE(...)` aggregates; we skip those when the key is checked.
- `GROUP BY (YEAR(?d))` without `AS` loses the expression (`[None]`). We judge the projection by what reaches the output instead.
- Aggregate result names (`__agg_1__`, …) restart in every subquery. Each `AggregateJoin` overwrites them, which only works because the traversal is post-order.
- `translateExists` assigns the translated pattern of `EXISTS` / `NOT EXISTS` to the *attribute* `graph` of the node (`CompValue` has no `__setattr__`), so `node.items()` still holds the raw parse tree, from which the inner `FILTER`s have already been removed. Every walker goes through `exists_pattern(node)` for these nodes; walking the items would miss the `GRAPH` variables, the identifier literals and the filters inside an `EXISTS` (that was a complete bypass until 17 October).
- `pyparsing` must stay at 3.2.0. Newer versions flood the logs with warnings.

## 8. Backlog for module D (most useful first)

1. **Reconcile corpus status after a fresh run-machine LLM evaluation.** Locally, #01 and #08 have parse errors and #07 is accepted by current Guard/Judge, unlike its committed status. Review sanitized post-retry outcomes and logs; do not infer the cause from the corpus alone.
2. **Measure retry value.** The Judge already provides inference-per-code-pattern and `COUNT(DISTINCT ?g)` feedback, and the parser inserts standard prefixes. Use fresh run-machine logs to identify which diagnostics trigger retries and whether the retries improve results.
3. **Catch Virtuoso-only errors statically.** Two LLM queries passed our parser but failed in Virtuoso, for example with `NULL` or a `GRAPH` nested inside `GRAPH`. We may not ask the database at runtime. The patterns found by the query eval (`MEDIAN`, `OVER`, a declared `bif:` prefix, `xsd:year()`, ungrouped variables) are listed in [SPARQL_LLM_VALIDATOR.md](SPARQL_LLM_VALIDATOR.md).
4. **Continue synthetic interaction tests only for identified gaps** in nested graph scope, filters, binds, aggregates, grouping, and property paths. Existing tests cover identity transforms, nested patient projections, `FROM NAMED`, `OPTIONAL`/`UNION`, inverse/alternative paths, and combined nested graph/path/filter/bind/aggregate behavior.
5. **Confirm current policy-dependent behavior:** the current code rejects federated `SERVICE` and groups filtered to exactly one patient. Ask whether those restrictions are intended and whether a larger minimum group size is required; do not extend either rule before the decision.
6. **Encode remaining gray zones** once the organizers answer: fine-grained grouping and `MIN`/`MAX`.
7. **Later, with module B:** performance rewrites for the slow shapes (README, section 11).

## Latest offline verification

On 17 October 2026, the focused Guard, SPARQL, Judge, pipeline/API, context, and profile-comparison tests passed **182 tests**. The full suite passed **191 tests**, with one existing Starlette/anyio deprecation warning. These results are for an uncommitted working tree based on `d9235ba2984e66e0ab847be924a1ae7d3fb595e8`; they do not verify real RDF graphs or Virtuoso behavior. The SERVICE check now explicitly reports R4, and its regression test covers the reason, retry hint, local-query near-miss, and no-retry behavior for flagged intent.

Later on 17 October (fixes #11, #12 and #13 of [FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md), plus the EXISTS quirk above): `tests/test_guard.py` and `tests/test_guard_pinning.py` pass **144 tests**. Measured offline with `scripts/check_query.py`'s machinery on 340 queries (the 16 official examples, the 37 corpus queries, the 45 dev reference queries, and the 242 generated queries and first tries of the dev rows of the 17 October query and pipeline reports): **zero verdict changes**; 15 messages changed (12 grouping violations gained their how-to sentence, 3 R1 messages were reworded). The 14 pinning bypasses of the prototype and the 2 EXISTS bypasses all become R1; the 13 house-style shapes stay compliant.
