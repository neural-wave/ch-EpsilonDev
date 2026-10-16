"""Import the official example queries into data/examples.jsonl.

Reads queries-example.md from the organizers' release folder and adds the worked example from the
challenge brief. Usage:
    python scripts/import_examples.py --release-dir ../hackathon-virtuoso-release
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from nl2sparql.config import REPO_ROOT

HEADING = re.compile(r"^(\d+)\.\s+(.+?)\s*$")
QUERY_START = re.compile(r"^(PREFIX|SELECT|CONSTRUCT|ASK|DESCRIBE|DEFINE)\b")
UNTAGGED_NON_COMPLIANT = {15}  # "Exposing identifiers" is not tagged in its title

SPEC_EXAMPLE = {
    "id": "spec-01",
    "question": "How many patients have a Billed Diagnosis for Type 2 diabetes mellitus "
    "(non-insulin-dependent diabetes mellitus)?",
    "sparql": """DEFINE sql:select-option "order"
PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT (COUNT(?pseudo_id) AS ?countResult)
WHERE {
    {
        SELECT DISTINCT(?g AS ?pseudo_id)
        WHERE {
            GRAPH ?g {
                ?diagnosis0_code rdf:type <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/E11>
                    OPTION(inference "https://biomedit.ch/rdf/sphn-resource/global") .
                ?diagnosis sphn:hasCode ?diagnosis0_code .
                ?diagnosis rdf:type sphn:BilledDiagnosis .
            }
        }
    }
}
""",
    "compliant": True,
    "source": "challenge brief",
    "title": "Type 2 diabetes patients",
}


def parse_markdown(markdown: str) -> list[dict]:
    examples: list[dict] = []
    current: dict | None = None
    for line in markdown.splitlines():
        heading = HEADING.match(line)
        if heading and not line.startswith(" "):
            current = {"number": int(heading.group(1)), "title": heading.group(2), "question": None, "lines": []}
            examples.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if current["question"] is None and not current["lines"] and stripped.startswith('"'):
            current["question"] = stripped.strip('"')
        elif current["lines"] or QUERY_START.match(stripped):
            current["lines"].append(line.rstrip())
    rows = []
    for example in examples:
        title = re.sub(r"\s*-\s*NON COMPLIANT\s*$", "", example["title"], flags=re.IGNORECASE)
        tagged = "NON COMPLIANT" in example["title"].upper()
        rows.append({
            "id": f"official-{example['number']:02d}",
            "question": example["question"],
            "sparql": "\n".join(example["lines"]).strip() + "\n",
            "compliant": not (tagged or example["number"] in UNTAGGED_NON_COMPLIANT),
            "source": "official",
            "title": title,
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release-dir", type=Path, default=REPO_ROOT.parent / "hackathon-virtuoso-release")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "examples.jsonl")
    args = parser.parse_args()
    rows = parse_markdown((args.release_dir / "queries-example.md").read_text(encoding="utf-8"))
    rows.append(SPEC_EXAMPLE)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    compliant = sum(row["compliant"] for row in rows)
    print(f"wrote {len(rows)} examples ({compliant} compliant, {len(rows) - compliant} non-compliant) to {args.out}")


if __name__ == "__main__":
    main()
