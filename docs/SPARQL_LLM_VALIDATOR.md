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
