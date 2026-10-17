"""Build assets/value_sets.json: the few coded values each class property uses in the data.

For example CivilStatus.hasCode takes six SNOMED CT concepts, and AdverseEvent.hasOutcome takes SPHN
individuals such as Fatal or Recovered. The schema cards show these values, so the query agent can
write ?status sphn:hasCode ?c . ?c a snomed:<concept> instead of guessing the code.

Labels come from restricted terminologies (SNOMED CT), so the output stays in assets/ (git-ignored)
and must never be committed or shared. Script time only: the API never queries the database.
    python scripts/build_value_sets.py --endpoint http://localhost:8890/sparql
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.sparql.prefixes import INFERENCE_RULE_SET, SPHN, STANDARD_PREFIXES
from nl2sparql.virtuoso import select

MAX_VALUES = 15  # larger sets (diagnoses, procedures) are left to the terminology index
INDIVIDUAL = STANDARD_PREFIXES["sphn-individual"]
SEMANTIC_TAG_RE = re.compile(r"\s*\([^()]*\)\s*$")  # "Married (finding)" -> "Married"

CODED_VALUES = f"""SELECT ?cls ?prop ?value (COUNT(*) AS ?n) WHERE {{
  GRAPH ?g {{ ?x a ?cls ; ?prop ?code . ?code a ?value }}
  FILTER(STRSTARTS(STR(?g), "http://example.org/graph/"))
  FILTER(STRSTARTS(STR(?cls), "{SPHN}") && STRSTARTS(STR(?prop), "{SPHN}"))
  FILTER(!STRSTARTS(STR(?value), "{SPHN}"))
}} GROUP BY ?cls ?prop ?value"""

INDIVIDUAL_VALUES = f"""SELECT ?cls ?prop ?value (COUNT(*) AS ?n) WHERE {{
  GRAPH ?g {{ ?x a ?cls ; ?prop ?value }}
  FILTER(STRSTARTS(STR(?g), "http://example.org/graph/"))
  FILTER(STRSTARTS(STR(?cls), "{SPHN}") && STRSTARTS(STR(?prop), "{SPHN}"))
  FILTER(STRSTARTS(STR(?value), "{INDIVIDUAL}"))
}} GROUP BY ?cls ?prop ?value"""


def compact(iri: str) -> str:
    for prefix, namespace in sorted(STANDARD_PREFIXES.items(), key=lambda item: -len(item[1])):
        if iri.startswith(namespace) and namespace not in ("bif:",):
            return f"{prefix}:{iri[len(namespace):]}"
    return f"<{iri}>"


def labels_query(iris: list[str]) -> str:
    values = " ".join(f"<{iri}>" for iri in iris)
    return f"""SELECT ?value ?label WHERE {{
  GRAPH <{INFERENCE_RULE_SET}> {{ ?value <http://www.w3.org/2000/01/rdf-schema#label> ?label }}
  VALUES ?value {{ {values} }}
}}"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=Settings.from_env().virtuoso_endpoint)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "assets" / "value_sets.json")
    args = parser.parse_args()

    found: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for query in (CODED_VALUES, INDIVIDUAL_VALUES):
        for row in select(args.endpoint, query).rows:
            found[(row["cls"][len(SPHN):], row["prop"][len(SPHN):])][row["value"]] = int(row["n"])
    small = {key: values for key, values in found.items() if len(values) <= MAX_VALUES}

    concepts = sorted({iri for values in small.values() for iri in values if not iri.startswith(INDIVIDUAL)})
    labels: dict[str, str] = {}
    for start in range(0, len(concepts), 200):
        for row in select(args.endpoint, labels_query(concepts[start:start + 200])).rows:
            labels.setdefault(row["value"], SEMANTIC_TAG_RE.sub("", row["label"]))

    classes: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for (cls, prop), values in sorted(small.items()):
        classes[cls][prop] = [
            {"value": compact(iri), "label": labels.get(iri) or iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1], "count": count}
            for iri, count in sorted(values.items(), key=lambda item: -item[1])
        ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"classes": classes}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{sum(len(props) for props in classes.values())} properties with at most {MAX_VALUES} values "
          f"({len(found) - len(small)} larger ones left to the terminology index) -> {args.out}")


if __name__ == "__main__":
    main()
