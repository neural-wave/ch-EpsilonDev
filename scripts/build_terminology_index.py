"""Build assets/terminology_index.jsonl: code IRIs with their labels, for the context step.

Takes ICD-10-GM and CHOP codes (unversioned IRIs) plus the SNOMED CT and LOINC concepts that occur
in the patient data, with their labels from the ontology graph. The output derives from restricted
terminologies: it stays in assets/ (git-ignored) and must never be committed or shared.
    python scripts/build_terminology_index.py --endpoint http://localhost:8890/sparql
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.sparql.prefixes import INFERENCE_RULE_SET
from nl2sparql.virtuoso import select

RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
CLASSIFICATIONS = {
    "ICD-10-GM": "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/",
    "CHOP": "https://biomedit.ch/rdf/sphn-resource/chop/",
}
CONCEPT_SYSTEMS = {
    "SNOMED CT": "http://snomed.info/id/",
    "LOINC": "https://loinc.org/rdf/",
}
DEFAULT_ENGLISH_LABELS = REPO_ROOT / "assets" / "english_terminology_labels.jsonl"


def load_english_labels(path: Path) -> dict[tuple[str, str], set[str]]:
    """Load a local restricted mapping keyed by terminology system and code."""
    if not path.exists():
        return {}
    labels: dict[tuple[str, str], set[str]] = defaultdict(set)
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        system = row["system"]
        code = row["code"]
        names = row["labels"]
        if system not in CLASSIFICATIONS:
            raise ValueError(f"Unsupported terminology system on line {line_number}: {system}")
        if not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names):
            raise ValueError(f"labels must be a non-empty list of strings on line {line_number}")
        labels[(system, code)].update(name.strip() for name in names)
    return labels


def classification_query(namespace: str) -> str:
    # Unversioned codes only: <.../icd-10-gm/E11>, not <.../icd-10-gm/2026/E11>.
    return f"""SELECT ?c ?label WHERE {{
  GRAPH <{INFERENCE_RULE_SET}> {{ ?c <{RDFS_LABEL}> ?label }}
  FILTER(STRSTARTS(STR(?c), "{namespace}"))
  FILTER(!CONTAINS(STRAFTER(STR(?c), "{namespace}"), "/"))
}}"""


def used_concepts_query() -> str:
    starts = " || ".join(f'STRSTARTS(STR(?concept), "{ns}")' for ns in CONCEPT_SYSTEMS.values())
    return f"""SELECT ?concept ?label WHERE {{
  {{ SELECT DISTINCT ?concept WHERE {{
      GRAPH ?g {{ ?code a ?concept }}
      FILTER(STRSTARTS(STR(?g), "http://example.org/graph/"))
      FILTER({starts})
  }} }}
  GRAPH <{INFERENCE_RULE_SET}> {{ ?concept <{RDFS_LABEL}> ?label }}
}}"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=Settings.from_env().virtuoso_endpoint)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "assets" / "terminology_index.jsonl")
    parser.add_argument("--english-labels", type=Path, default=DEFAULT_ENGLISH_LABELS)
    args = parser.parse_args()
    labels: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for system, namespace in CLASSIFICATIONS.items():
        rows = select(args.endpoint, classification_query(namespace)).rows
        for row in rows:
            labels[(row["c"], row["c"][len(namespace):], system)].add(row["label"])
        print(f"  {system}: {len(rows)} labels")
    rows = select(args.endpoint, used_concepts_query()).rows
    for row in rows:
        system, namespace = next((s, ns) for s, ns in CONCEPT_SYSTEMS.items() if row["concept"].startswith(ns))
        labels[(row["concept"], row["concept"][len(namespace):], system)].add(row["label"])
    print(f"  concepts used in the data: {len(rows)} labels")
    english_labels = load_english_labels(args.english_labels)
    for (system, code), names in english_labels.items():
        namespace = CLASSIFICATIONS[system]
        labels[(namespace + code, code, system)].update(names)
    if english_labels:
        print(f"  English labels: {sum(len(names) for names in english_labels.values())}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for (iri, code, system), names in sorted(labels.items()):
            handle.write(json.dumps({"iri": iri, "code": code, "system": system, "labels": sorted(names)}, ensure_ascii=False) + "\n")
    print(f"wrote {len(labels)} codes to {args.out}")


if __name__ == "__main__":
    main()
