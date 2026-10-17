"""Profile the patient data in Virtuoso and write the files the guard, judge and context read.

Writes data/property_kinds.json (what each property points to) and data/schema_cards.json (which
classes exist, with their properties: datatype, the classes a link reaches, the terminology of a
coded property, the members of a value set). Neither file contains terminology content, so both are
committed. The cards are measured class by class, each query under Virtuoso's anytime timeout, so a
partial answer is an error instead of a wrong card. Run against the dev database:
    python scripts/profile_data.py --endpoint http://localhost:8890/sparql
    python scripts/profile_data.py --cards-only   # keep property_kinds.json, rebuild the cards (2 min)
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

import httpx

from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.sparql.prefixes import INFERENCE_RULE_SET, SPHN, STANDARD_PREFIXES
from nl2sparql.virtuoso import select

PATIENT_GRAPHS = 'FILTER(STRSTARTS(STR(?g), "http://example.org/graph/"))'
INDIVIDUAL = STANDARD_PREFIXES["sphn-individual"]
RESOURCE = "https://biomedit.ch/rdf/sphn-resource/"
OWL = "http://www.w3.org/2002/07/owl#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
ANYTIME_MS = 30000  # Virtuoso's anytime timeout per query; the per-class queries take well under a second
ACCEPT_JSON = "application/sparql-results+json"
NAMESPACE_RE = re.compile(r"^(.*[/#](?:[A-Za-z]+_)?)[^/#]*$")

# The terminology a code node's type belongs to, by namespace. The names are the "code_system"
# values of the cards (the judge compares them with the namespaces a query uses).
TERMINOLOGIES = (
    (STANDARD_PREFIXES["icd-10-gm"], "ICD-10-GM"),
    (STANDARD_PREFIXES["chop"], "CHOP"),
    (STANDARD_PREFIXES["snomed"], "SNOMED CT"),
    (STANDARD_PREFIXES["loinc"], "LOINC"),
    ("https://biomedit.ch/rdf/sphn-resource/loinc/", "LOINC"),
    (STANDARD_PREFIXES["ucum"], "UCUM"),
    (STANDARD_PREFIXES["atc"], "ATC"),
    ("https://biomedit.ch/rdf/sphn-resource/atc/", "ATC"),
    (STANDARD_PREFIXES["geno"], "GENO"),
    (STANDARD_PREFIXES["ordo"], "ORDO"),
)

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

# Per class: how often each property occurs. Per class and property: the types of the objects it
# reaches (record classes and code concepts) and the value-set entries it takes. The property is
# fixed on purpose: with a variable predicate joined to `?o a ?type`, Virtuoso drops some of the
# (property, type) groups (Assay.hasSample lost every type on the dev database).
CLASS_PROPERTY_QUERY = f"""SELECT ?p (COUNT(*) AS ?n)
WHERE {{ GRAPH ?g {{ ?s a <{{cls}}> ; ?p ?o }} {PATIENT_GRAPHS} FILTER(?p != <{RDF_TYPE}>) }}
GROUP BY ?p"""

OBJECT_TYPE_QUERY = f"""SELECT ?type (COUNT(*) AS ?n)
WHERE {{ GRAPH ?g {{ ?s a <{{cls}}> ; <{{prop}}> ?o . ?o a ?type }} {PATIENT_GRAPHS} }}
GROUP BY ?type"""

INDIVIDUAL_QUERY = f"""SELECT ?o (COUNT(*) AS ?n)
WHERE {{ GRAPH ?g {{ ?s a <{{cls}}> ; <{{prop}}> ?o }} {PATIENT_GRAPHS} FILTER(isIRI(?o) && STRSTARTS(STR(?o), "{INDIVIDUAL}")) }}
GROUP BY ?o"""

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


def terminology(type_iri: str) -> str | None:
    """The code system a code node's type belongs to; "generic" for sphn:Code, None for a record
    class, the namespace itself for a terminology the table does not name."""
    if type_iri == f"{SPHN}Code":
        return "generic"
    if type_iri.startswith(SPHN):
        return None
    for namespace, name in TERMINOLOGIES:
        if type_iri.startswith(namespace):
            return name
    # The namespace, keeping an OBO-style prefix of the local part: .../obo/SO_0001483 -> .../obo/SO_
    return NAMESPACE_RE.match(type_iri).group(1)


def card_properties(property_rows: list[dict], type_rows: list[dict], individual_rows: list[dict], kinds: dict) -> dict:
    """The property entries of one class card: the property counts of the class, and the object
    types and value-set entries of each property, each row carrying its property under "p"."""
    entries = {}
    for row in property_rows:
        info = kinds.get(row["p"], {})
        entry = {"kind": info.get("kind", "unknown"), "count": int(row["n"])}
        if info.get("datatype"):
            entry["datatype"] = info["datatype"].rsplit("#", 1)[-1]
        entries[row["p"]] = entry
    targets: dict[str, Counter] = {}
    systems: dict[str, Counter] = {}
    for row in type_rows:
        if row["p"] not in entries:
            continue
        system = terminology(row["type"])
        if system is None:
            targets.setdefault(row["p"], Counter())[local(row["type"])] += int(row["n"])
        else:
            systems.setdefault(row["p"], Counter())[system] += int(row["n"])
    for prop, counter in targets.items():
        ranked = [target for target, _ in counter.most_common()]
        entries[prop]["target"] = ranked[0]  # the most frequent class, as before
        if len(ranked) > 1:
            entries[prop]["targets"] = ranked
    for prop, counter in systems.items():
        if "generic" in counter:  # sphn:Code typed nodes: the judge reads that from "target" too
            entries[prop].setdefault("target", "Code")
        entries[prop]["code_system"] = counter.most_common(1)[0][0]
        if len(counter) > 1:
            entries[prop]["code_systems"] = dict(counter.most_common())
    values: dict[str, Counter] = {}
    for row in individual_rows:
        if row["p"] in entries:
            values.setdefault(row["p"], Counter())[row["o"][len(INDIVIDUAL):]] += int(row["n"])
    for prop, counter in values.items():
        entries[prop]["values"] = dict(counter.most_common())
    return {local(prop): entry for prop, entry in sorted(entries.items(), key=lambda item: local(item[0]))}


def schema_cards(class_rows: list[dict], per_class: dict[str, dict], terms: list[dict]) -> dict:
    classes = {
        local(row["class"]): {"instances": int(row["instances"]), "patients": int(row["patients"]),
                              "properties": per_class.get(row["class"], {})}
        for row in class_rows
    }
    return {
        "classes": dict(sorted(classes.items())),
        "schema_terms": sorted(local(row["term"]) for row in terms),
    }


def select_rows(endpoint: str, query: str, anytime_ms: int = ANYTIME_MS) -> list[dict]:
    """Rows of a SELECT run under Virtuoso's anytime timeout; a partial answer (HTTP 206) is an error."""
    response = httpx.post(endpoint, data={"query": query, "timeout": str(anytime_ms)},
                          headers={"Accept": ACCEPT_JSON}, timeout=anytime_ms / 1000 + 30)
    response.raise_for_status()
    if response.status_code == 206 or response.headers.get("X-SQL-State") == "S1TAT":
        raise RuntimeError(f"Virtuoso returned a partial result after {anytime_ms} ms for:\n{query}")
    bindings = response.json().get("results", {}).get("bindings", [])
    return [{name: cell.get("value", "") for name, cell in binding.items()} for binding in bindings]


def run(endpoint: str, name: str, query: str) -> list[dict]:
    started = time.perf_counter()
    rows = select_rows(endpoint, query)
    print(f"  {name}: {len(rows)} rows in {time.perf_counter() - started:.0f}s")
    return rows


def profile_classes(endpoint: str, class_rows: list[dict], kinds: dict) -> dict[str, dict]:
    started = time.perf_counter()
    per_class = {}
    for index, row in enumerate(class_rows, start=1):
        cls = row["class"]
        property_rows = select_rows(endpoint, CLASS_PROPERTY_QUERY.replace("{cls}", cls))
        type_rows, individual_rows = [], []
        for prop in (entry["p"] for entry in property_rows):
            kind = kinds.get(prop, {}).get("kind", "unknown")
            if kind in ("resource", "mixed", "unknown"):
                query = OBJECT_TYPE_QUERY.replace("{cls}", cls).replace("{prop}", prop)
                type_rows += [{"p": prop, **found} for found in select_rows(endpoint, query)]
            if kind in ("individual", "mixed", "unknown"):
                query = INDIVIDUAL_QUERY.replace("{cls}", cls).replace("{prop}", prop)
                individual_rows += [{"p": prop, **found} for found in select_rows(endpoint, query)]
        per_class[cls] = card_properties(property_rows, type_rows, individual_rows, kinds)
        if index % 20 == 0 or index == len(class_rows):
            print(f"  class properties: {index}/{len(class_rows)} classes in {time.perf_counter() - started:.0f}s")
    return per_class


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=Settings.from_env().virtuoso_endpoint)
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--cards-only", action="store_true",
                        help="rebuild schema_cards.json only, reading property kinds from the existing property_kinds.json")
    args = parser.parse_args()
    print(f"profiling {args.endpoint}")
    kinds_path = args.data_dir / "property_kinds.json"
    if args.cards_only:
        kinds = json.loads(kinds_path.read_text(encoding="utf-8"))
    else:
        # One pass over every triple: too slow for the anytime timeout, so it runs without one.
        started = time.perf_counter()
        kinds = property_kinds(select(args.endpoint, PROPERTY_QUERY).rows)
        print(f"  properties: {len(kinds)} rows in {time.perf_counter() - started:.0f}s")
    class_rows = run(args.endpoint, "classes", CLASS_QUERY)
    cards = schema_cards(
        class_rows,
        profile_classes(args.endpoint, class_rows, kinds),
        run(args.endpoint, "schema terms", SCHEMA_TERMS_QUERY),
    )
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if not args.cards_only:
        kinds_path.write_text(json.dumps(kinds, indent=1) + "\n", encoding="utf-8")
    (args.data_dir / "schema_cards.json").write_text(json.dumps(cards, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(kinds)} properties and {len(cards['classes'])} classes to {args.data_dir}")


if __name__ == "__main__":
    main()
