"""Explain the guard's verdict on one SPARQL query: query form, what each variable can hold, broken rules.

    python scripts/check_query.py path/to/query.rq
    pbpaste | python scripts/check_query.py -
"""
from __future__ import annotations

import sys
from pathlib import Path

from nl2sparql.config import REPO_ROOT
from nl2sparql.guard import ComplianceGuard, PropertyKinds, RuleSettings
from nl2sparql.guard.provenance import analyse
from nl2sparql.sparql.parse import parse_sparql


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    text = sys.stdin.read() if sys.argv[1] == "-" else Path(sys.argv[1]).read_text(encoding="utf-8")
    kinds = PropertyKinds.load(REPO_ROOT / "data" / "property_kinds.json")
    parsed = parse_sparql(text)
    print(f"query form: {parsed.kind}" + (f"  ({parsed.error})" if parsed.error else ""))
    if parsed.query is not None:
        facts = analyse(parsed.algebra, kinds, RuleSettings().ontology_graphs)
        print(f"touches patient data: {facts.touches_patient_data}")
        print("what each variable can hold (internal __agg_N__ = aggregate results):")
        for variable, level in sorted(facts.sensitivity.items(), key=lambda item: (-item[1], str(item[0]))):
            print(f"  ?{variable:<28} {level.name}")
    violations = ComplianceGuard(kinds).check_query(text)
    print("verdict:", "compliant (no rule broken)" if not violations else "NON-COMPLIANT")
    for violation in violations:
        print(f"  {violation.rule}: {violation.message}")


if __name__ == "__main__":
    main()
