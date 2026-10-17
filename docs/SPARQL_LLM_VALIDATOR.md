# sparql-llm validator: evaluation for the judge

For whoever owns the judge (module D). It was evaluated offline on 2026-10-17 and is **not integrated**. The decision is yours.

## What it is

[sparql-llm](https://github.com/sib-swiss/sparql-llm) is a toolkit from SIB, the challenge organizers, under the MIT license. We tested version 0.1.4.

Its function `validate_sparql_with_void` (in `sparql_llm/validate_sparql.py`) checks every typed subject in a query:
- that its class exists;
- that every predicate used on it is one this class has in the data.

It also follows predicate targets to infer the type of linked nodes. Each problem comes back as a message with the valid alternatives, for example:

> Subject ?event with type `sphn:AdverseEvent` does not support the predicate `sphn:hasOutcomeCode`. It can have the following predicates: `sphn:hasAdministrativeCase`, `sphn:hasCode`, `sphn:hasConsequences`, …

Our judge only checks that each `sphn:` term occurs somewhere in the data, so it cannot see a real predicate used on the wrong class.

## Results (offline, no database access)

| Queries | Result |
|---|---|
| The 16 official examples | **0 issues**: no false alarms |
| The 66 final queries Qwen3.5-9B wrote in our evals (`logs/requests.jsonl`) | **13 with schema issues; our judge passed 9 of them** |

Issues our judge missed, and the wrong results they explain:

| Question | Validator message (short) |
|---|---|
| #04 "born in a country they aren't a citizen of" | The country class it used does not exist. The query returned 0 instead of 1,894 |
| #10 "babies born early" | `GestationalAgeAtBirth` has no `hasCode`; it has `hasQuantity` |
| "Average age of patients with cholera" | `Age` has no `hasValue`; it has `hasQuantity` |
| "Each lab result value with its date" | `LabResult` has no `hasValue` |

It does **not** catch syntax errors or Virtuoso-only errors: `||` between graph patterns, `NULL`, ungrouped projected variables, aggregates in the wrong place. Those caused 5 of the 9 queries that still failed after the retry.

## How to integrate it without querying the database

1. **Do not add the `sparql-llm` package.** Even without its chat extras it pulls `fastembed` (ONNX), `qdrant-client`, `pandas`, `langchain-core` and `mcp`. Instead, copy `validate_sparql.py`, keeping its MIT notice, and add `curies` to `requirements.txt`.
2. **Replace `sparql_llm.utils` with a small local module.** It must provide `get_prefix_converter`, `compress_list`, the `SchemaDict` and `EndpointsSchemaDict` types, a logger, and a `get_schema_for_endpoint` that returns the precomputed schema and never makes a network call.
3. **Build the schema once from `data/schema_cards.json`** as `{class IRI: {property IRI: [target class IRI or XSD datatype IRI]}}`. That covers 164 classes.
4. **Strip the Virtuoso extensions first** (`DEFINE`, `OPTION (inference …)`) with `nl2sparql.sparql.parse.strip_virtuoso_extensions`.
5. **Call it:**

   ```python
   validate_sparql_with_void(query, "sphn-data", prefix_converter=converter,
                             endpoints_void_dict={"sphn-data": schema})
   ```

**Pitfalls we hit:**
- **`GRAPH ?g` read as `SERVICE`.** It treats some `GRAPH ?g { … }` blocks as federated `SERVICE` calls and asks for the schema of an endpoint named `?g`. The original library would try a network call there. We have one dataset, so answer every endpoint with the same schema (step 2).
- **Codes.** Messages about terminology classes (`icd-10-gm:`, `snomed:`, …) are expected, because codes are not in the schema. Filter them out.
- **Long messages.** "Available classes are: …" lists all 164 classes. Shorten it to the closest names before it goes into the retry feedback, or the retry gets very expensive.
- **Recursion limit.** Some queries produce "Recursion limit reached for subject …". Ignore that message.

Santiago has the prototype used for these numbers (a vendored copy and an offline runner) if you want to reproduce them.

## Virtuoso-only errors the judge could catch in code

The query eval on 47 reference questions (`eval/run_query_eval.py`, 2026-10-17) found queries that our parser accepts but Virtuoso rejects. Each one made an allowed request fail, and some were blocked as not compliant. A plain text check before the guard would turn them into a retry with a precise hint:

| Pattern in the query | Virtuoso's error | Hint for the retry |
|---|---|---|
| `MEDIAN(`, `OVER (` | syntax error | Not supported: compute it differently (e.g. a subquery) |
| `UNBOUNDED`, `NULL` | syntax error | `SUM(IF(<condition>, 1, 0))` for a condition on a per-patient number; `OPTIONAL { GRAPH ?g { … } BIND(?g AS ?x) }` and `COUNT(DISTINCT ?x)` for an absent pattern |
| `PREFIX bif: <http…>` | prefix `bif:` is reserved (SP030 for the W3C and `/sparql/bif#` namespaces) | The judge rewrites every `bif:` declaration to `<bif:>` (`fix_standard_prefixes`) |
| `xsd:year(` | undefined procedure | Use YEAR() |
| `\|\|` between graph patterns | syntax error | Use UNION |
| A selected variable that is neither grouped nor aggregated | SP031 | Add it to GROUP BY or aggregate it |
| `BIND(... AS ?x)` on a variable already in scope (q-059, q-071, q-073; official-10 and q-042 ran and returned wrong results) | `Virtuoso 37000 Error SP031: SPARQL compiler: Alias ?decade is defined twice` (HTTP 400) or `Virtuoso 42S22 Error SQ149: The column 'test_count' was specified multiple times for 's_61_22'` (HTTP 500) | Bind a new variable name: a variable bound by a pattern cannot be re-bound |
| `YEAR()` on a record IRI, `DAY()` on a datediff result, `bif:datediff` on a record (q-005, q-069, q-074, official-01) | `Virtuoso 22007 Error DT001: Function year needs a datetime, date or time as argument 1, not an arg of type IRI_ID (243)`; the same with `day` / `INTEGER (189)` and `datediff` / `argument 3` / `VARCHAR (182)` | Apply the date function to the record's date property from the card, e.g. `?death sphn:hasReportDateTime ?reported . FILTER (YEAR(?reported) >= 2015)` |
| `COUNT(DISTINCT ?alias)` over a SELECT alias, `SUM(COUNT(...))` (q-055, q-066) | `Virtuoso 37000 Error SQ200: Aggregate function not allowed in context` | Compute the inner aggregate in a sub-select and aggregate its result |
| `COUNT(DISTINCT ?g)` and `COUNT(DISTINCT ?x)` with an unconditional `BIND(?g AS ?x)` (q-013) | `Virtuoso 22023 Error SR066: Unsupported case in CONVERT (UNK_DV_TYPE -> VARCHAR)`, a Virtuoso bug on two `COUNT(DISTINCT)` over the same column, written after a partial result; the share is 100% by construction anyway | Bind the alias inside the OPTIONAL that holds the condition, so the two counts differ |
| `PREFIX icd-10-gm:` redeclared with the OBO namespace (which also bypasses the judge's inference check), an unbound GROUP BY, an empty cohort as denominator (q-060) | `Virtuoso 22012 Error SR083: Division by zero` | Keep the standard namespaces; guard the division (`IF(?n > 0, ..., 0)`) |

Query prompt v3 (`src/nl2sparql/agents/prompts.py`) asks the model to avoid all of these. A check in the judge would still catch the ones it writes anyway. The texts above are what the dev Virtuoso answered on 17 October for the generated queries of `eval/reports/query-20261017-140904.json` (FAILURE_ANALYSIS.md §3.2); since that day the query eval keeps them in each row's `execution_error`.

### Frequency on the 87-question baseline (17 October, 14:10, dev split)

19 generated queries that our parser accepted were rejected by Virtuoso:

| Count | Virtuoso error | Example |
|---|---|---|
| 2 | syntax error at `UNION` before `{` | q-008 |
| 2 | syntax error at `true` before `}` (a bare boolean where a pattern was expected) | q-018 |
| 2 | parentheses not balanced | q-027 |
| 2 | SQ200 aggregate function not allowed in context | q-055 |
| 3 | DT001: `YEAR()` on a record IRI (`not an arg of type IRI_ID`), `DAY()` on a datediff result (`INTEGER`), `bif:datediff` on a record (`VARCHAR`) | q-005, q-069, q-074 |
| 2 | SQ149 (SQL state 42S22) `The column 'x' was specified multiple times`: a `BIND(... AS ?x)` on a variable already in scope | q-071, q-073 (also SP031 `Alias ?x is defined twice` on q-059) |
| 1 | syntax error at `FILTER` before `(` | q-010 |
| 1 | syntax error at `a` before `snomed:` and at `|` (an alternative path written wrongly) | q-022, q-041 |
| 1 | SR083 division by zero | q-060 |
| 1 | SR066 `Unsupported case in CONVERT (UNK_DV_TYPE -> VARCHAR)`: two `COUNT(DISTINCT)` over `?g` and its unconditional alias `BIND(?g AS ?x)`, a Virtuoso bug (not a literal compared with an IRI, as this table said at first) | q-013 |

Unbalanced parentheses, a duplicated alias, `UNION`/`FILTER` placement and a bare `true` are checkable in code before the guard. The date functions need the schema cards: the argument must be a date property of the record, not the record itself or a datediff result.
