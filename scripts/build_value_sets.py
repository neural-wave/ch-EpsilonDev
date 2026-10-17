"""Build assets/value_sets.json: the coded values each class property uses in the data.

For example CivilStatus.hasCode takes six SNOMED CT concepts, and AdverseEvent.hasOutcome takes SPHN
individuals such as Fatal or Recovered. The schema cards show these values, so the query agent can
write ?status sphn:hasCode ?c . ?c a snomed:<concept> instead of guessing the code. Coded sets above
MAX_VALUES members (diagnoses, procedures) are left to the terminology index; sphn-individual sets
are kept whole, however large (TherapeuticArea.hasSpecialtyName has 64 members).

Labels come from restricted terminologies (SNOMED CT), so the output stays in assets/ (git-ignored)
and must never be committed or shared. Script time only: the API never queries the database. Each
query runs class by class under Virtuoso's anytime timeout (about a minute on 2,000 patients).
    python scripts/build_value_sets.py --endpoint http://localhost:8890/sparql
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import httpx

from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.sparql.prefixes import INFERENCE_RULE_SET, SPHN, STANDARD_PREFIXES

MAX_VALUES = 15  # coded sets only: larger ones (diagnoses, procedures) are left to the terminology index
INDIVIDUAL = STANDARD_PREFIXES["sphn-individual"]
SEMANTIC_TAG_RE = re.compile(r"\s*\([^()]*\)\s*$")  # "Married (finding)" -> "Married"
PATIENT_GRAPHS = 'FILTER(STRSTARTS(STR(?g), "http://example.org/graph/"))'
ANYTIME_MS = 30000
ACCEPT_JSON = "application/sparql-results+json"

CLASSES = f"""SELECT DISTINCT ?cls WHERE {{
  GRAPH ?g {{ ?x a ?cls }} {PATIENT_GRAPHS} FILTER(STRSTARTS(STR(?cls), "{SPHN}"))
}}"""

# One query per class and property: with a variable predicate joined to `?code a ?value`, Virtuoso
# drops some of the (property, value) groups (see scripts/profile_data.py).
PROPERTIES = f"""SELECT DISTINCT ?prop WHERE {{
  GRAPH ?g {{ ?x a <{{cls}}> ; ?prop ?o }} {PATIENT_GRAPHS} FILTER(STRSTARTS(STR(?prop), "{SPHN}"))
}}"""

CODED_VALUES = f"""SELECT ?value (COUNT(*) AS ?n) WHERE {{
  GRAPH ?g {{ ?x a <{{cls}}> ; <{{prop}}> ?code . ?code a ?value }} {PATIENT_GRAPHS}
  FILTER(!STRSTARTS(STR(?value), "{SPHN}"))
}} GROUP BY ?value"""

INDIVIDUAL_VALUES = f"""SELECT ?value (COUNT(*) AS ?n) WHERE {{
  GRAPH ?g {{ ?x a <{{cls}}> ; <{{prop}}> ?value }} {PATIENT_GRAPHS}
  FILTER(STRSTARTS(STR(?value), "{INDIVIDUAL}"))
}} GROUP BY ?value"""


def compact(iri: str) -> str:
    for prefix, namespace in sorted(STANDARD_PREFIXES.items(), key=lambda item: -len(item[1])):
        if iri.startswith(namespace) and namespace not in ("bif:",):
            return f"{prefix}:{iri[len(namespace):]}"
    return f"<{iri}>"


def keep(values: dict[str, int]) -> bool:
    """Value sets the cards show: every sphn-individual set, coded sets of at most MAX_VALUES members."""
    return all(iri.startswith(INDIVIDUAL) for iri in values) or len(values) <= MAX_VALUES


def labels_query(iris: list[str]) -> str:
    values = " ".join(f"<{iri}>" for iri in iris)
    return f"""SELECT ?value ?label WHERE {{
  GRAPH <{INFERENCE_RULE_SET}> {{ ?value <http://www.w3.org/2000/01/rdf-schema#label> ?label }}
  VALUES ?value {{ {values} }}
}}"""


def select_rows(endpoint: str, query: str, anytime_ms: int = ANYTIME_MS) -> list[dict]:
    """Rows of a SELECT run under Virtuoso's anytime timeout; a partial answer (HTTP 206) is an error."""
    response = httpx.post(endpoint, data={"query": query, "timeout": str(anytime_ms)},
                          headers={"Accept": ACCEPT_JSON}, timeout=anytime_ms / 1000 + 30)
    response.raise_for_status()
    if response.status_code == 206 or response.headers.get("X-SQL-State") == "S1TAT":
        raise RuntimeError(f"Virtuoso returned a partial result after {anytime_ms} ms for:\n{query}")
    bindings = response.json().get("results", {}).get("bindings", [])
    return [{name: cell.get("value", "") for name, cell in binding.items()} for binding in bindings]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=Settings.from_env().virtuoso_endpoint)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "assets" / "value_sets.json")
    args = parser.parse_args()

    started = time.perf_counter()
    found: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    classes = sorted(row["cls"] for row in select_rows(args.endpoint, CLASSES))
    for cls in classes:
        for prop in sorted(row["prop"] for row in select_rows(args.endpoint, PROPERTIES.replace("{cls}", cls))):
            for query in (CODED_VALUES, INDIVIDUAL_VALUES):
                for row in select_rows(args.endpoint, query.replace("{cls}", cls).replace("{prop}", prop)):
                    found[(cls[len(SPHN):], prop[len(SPHN):])][row["value"]] = int(row["n"])
    print(f"  {len(classes)} classes profiled in {time.perf_counter() - started:.0f}s")
    kept = {key: values for key, values in found.items() if keep(values)}

    concepts = sorted({iri for values in kept.values() for iri in values if not iri.startswith(INDIVIDUAL)})
    labels: dict[str, str] = {}
    for start in range(0, len(concepts), 200):
        for row in select_rows(args.endpoint, labels_query(concepts[start:start + 200])):
            labels.setdefault(row["value"], SEMANTIC_TAG_RE.sub("", row["label"]))

    classes_out: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for (cls, prop), values in sorted(kept.items()):
        classes_out[cls][prop] = [
            {"value": compact(iri), "label": labels.get(iri) or iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1], "count": count}
            for iri, count in sorted(values.items(), key=lambda item: -item[1])
        ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"classes": classes_out}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{sum(len(props) for props in classes_out.values())} value sets (coded ones with at most {MAX_VALUES} values; "
          f"{len(found) - len(kept)} larger coded sets left to the terminology index) -> {args.out}")


if __name__ == "__main__":
    main()
