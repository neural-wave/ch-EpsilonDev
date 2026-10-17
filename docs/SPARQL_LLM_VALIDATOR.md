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
| `UNBOUNDED`, `NULL` | syntax error | Use OPTIONAL with BOUND() or COALESCE() |
| `PREFIX bif: <http…>` | prefix `bif:` is reserved | Do not declare `bif:` |
| `xsd:year(` | undefined procedure | Use YEAR() |
| `\|\|` between graph patterns | syntax error | Use UNION |
| A selected variable that is neither grouped nor aggregated | SP031 | Add it to GROUP BY or aggregate it |

Query prompt v3 (`src/nl2sparql/agents/prompts.py`) asks the model to avoid all of these. A check in the judge would still catch the ones it writes anyway.

### Frequency on the 87-question baseline (17 October, 14:10, dev split)

19 generated queries that our parser accepted were rejected by Virtuoso:

| Count | Virtuoso error | Example |
|---|---|---|
| 2 | syntax error at `UNION` before `{` | q-008 |
| 2 | syntax error at `true` before `}` (a bare boolean where a pattern was expected) | q-018 |
| 2 | parentheses not balanced | q-027 |
| 2 | SQ200 aggregate function not allowed in context | q-055 |
| 3 | DT001: `YEAR()`, `DAY()` or `bif:datediff` applied to a value that is not a date (`BirthDate` holds `hasYear`/`hasMonth`/`hasDay` parts, not a dateTime) | q-005, q-069, q-074 |
| 2 | 42S22: a selected alias defined twice | q-071, q-073 (also SP031 on q-059) |
| 1 | syntax error at `FILTER` before `(` | q-010 |
| 1 | syntax error at `a` before `snomed:` and at `|` (an alternative path written wrongly) | q-022, q-041 |
| 1 | SR083 division by zero | q-060 |
| 1 | SR066 unsupported CONVERT (a literal compared with an IRI) | q-013 |

Unbalanced parentheses, a duplicated alias, `UNION`/`FILTER` placement and a bare `true` are checkable in code before the guard. The date functions need the schema: a property whose datatype is `gYear` is not a `dateTime`.
