"""Profile the patient data in Virtuoso and write the files the guard, judge and context read.

Writes data/property_kinds.json (what each property points to) and data/schema_cards.json (which
classes exist, with their properties). Neither file contains terminology content, so both are
committed. Run against the dev database (about 3 minutes on 2,000 patients):
    python scripts/profile_data.py --endpoint http://localhost:8890/sparql
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.sparql.prefixes import INFERENCE_RULE_SET, SPHN
from nl2sparql.virtuoso import select

PATIENT_GRAPHS = 'FILTER(STRSTARTS(STR(?g), "http://example.org/graph/"))'
INDIVIDUAL = "https://biomedit.ch/rdf/sphn-schema/sphn/individual#"
RESOURCE = "https://biomedit.ch/rdf/sphn-resource/"
OWL = "http://www.w3.org/2002/07/owl#"

PROPERTY_QUERY = f"""SELECT ?p (COUNT(*) AS ?n)
  (SUM(IF(isLiteral(?o), 1, 0)) AS ?literal)
  (SUM(IF(isIRI(?o) && STRSTARTS(STR(?o), "{INDIVIDUAL}"), 1, 0)) AS ?individual)
  (SUM(IF(isIRI(?o) && STRSTARTS(STR(?o), "{RESOURCE}"), 1, 0)) AS ?resource)
  (SAMPLE(DATATYPE(?o)) AS ?datatype)
WHERE {{ GRAPH ?g {{ ?s ?p ?o }} {PATIENT_GRAPHS} }}
GROUP BY ?p"""

CLASS_QUERY = f"""SELECT ?class (COUNT(*) AS ?instances) (COUNT(DISTINCT ?g) AS ?patients)
WHERE {{ GRAPH ?g {{ ?s a ?class }} {PATIENT_GRAPHS} FILTER(STRSTARTS(STR(?class), "{SPHN}")) }}
GROUP BY ?class"""

CLASS_PROPERTY_QUERY = f"""SELECT ?class ?p (SAMPLE(?targetClass) AS ?target) (COUNT(*) AS ?n)
WHERE {{
  GRAPH ?g {{
    ?s a ?class ; ?p ?o .
    OPTIONAL {{ ?o a ?targetClass . FILTER(STRSTARTS(STR(?targetClass), "{SPHN}")) }}
  }}
  {PATIENT_GRAPHS}
  FILTER(STRSTARTS(STR(?class), "{SPHN}"))
  FILTER(?p != <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>)
}}
GROUP BY ?class ?p"""

SCHEMA_TERMS_QUERY = f"""SELECT DISTINCT ?term
WHERE {{
  GRAPH <{INFERENCE_RULE_SET}> {{ ?term a ?type }}
  FILTER(STRSTARTS(STR(?term), "{SPHN}"))
  FILTER(?type IN (<{OWL}Class>, <{OWL}ObjectProperty>, <{OWL}DatatypeProperty>))
}}"""


def property_kinds(rows: list[dict]) -> dict:
    kinds = {}
    for row in rows:
        n, literal, individual, resource = (int(row.get(key) or 0) for key in ("n", "literal", "individual", "resource"))
        if literal == n:
            kind = "literal"
        elif individual == n:
            kind = "individual"
        elif resource == n:
            kind = "resource"
        else:
            kind = "mixed"
        entry = {"kind": kind, "count": n}
        if kind == "literal" and row.get("datatype"):
            entry["datatype"] = row["datatype"]
        kinds[row["p"]] = entry
    return dict(sorted(kinds.items()))


def local(iri: str) -> str:
    return iri[len(SPHN):] if iri.startswith(SPHN) else iri


def schema_cards(class_rows: list[dict], class_property_rows: list[dict], kinds: dict, terms: list[dict]) -> dict:
    classes = {
        local(row["class"]): {"instances": int(row["instances"]), "patients": int(row["patients"]), "properties": {}}
        for row in class_rows
    }
    for row in class_property_rows:
        card = classes.get(local(row["class"]))
        if card is None:
            continue
        info = kinds.get(row["p"], {})
        entry = {"kind": info.get("kind", "unknown"), "count": int(row["n"])}
        if row.get("target"):
            entry["target"] = local(row["target"])
        if info.get("datatype"):
            entry["datatype"] = info["datatype"].rsplit("#", 1)[-1]
        card["properties"][local(row["p"])] = entry
    for card in classes.values():
        card["properties"] = dict(sorted(card["properties"].items()))
    return {
        "classes": dict(sorted(classes.items())),
        "schema_terms": sorted(local(row["term"]) for row in terms),
    }


def run(endpoint: str, name: str, query: str) -> list[dict]:
    started = time.perf_counter()
    rows = select(endpoint, query).rows
    print(f"  {name}: {len(rows)} rows in {time.perf_counter() - started:.0f}s")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=Settings.from_env().virtuoso_endpoint)
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    args = parser.parse_args()
    print(f"profiling {args.endpoint}")
    kinds = property_kinds(run(args.endpoint, "properties", PROPERTY_QUERY))
    cards = schema_cards(
        run(args.endpoint, "classes", CLASS_QUERY),
        run(args.endpoint, "class properties", CLASS_PROPERTY_QUERY),
        kinds,
        run(args.endpoint, "schema terms", SCHEMA_TERMS_QUERY),
    )
    args.data_dir.mkdir(parents=True, exist_ok=True)
    (args.data_dir / "property_kinds.json").write_text(json.dumps(kinds, indent=1) + "\n", encoding="utf-8")
    (args.data_dir / "schema_cards.json").write_text(json.dumps(cards, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(kinds)} properties and {len(cards['classes'])} classes to {args.data_dir}")


if __name__ == "__main__":
    main()
