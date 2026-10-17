"""Proposed guard hint change: when a GROUP BY key (or a projected value) is instance-level, say where it
comes from and what to group by instead. Prototype over the real cases; not in the repo."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path("/Users/santiagofigueroa/Desktop/Hackathon/nl2sparql")
sys.path.insert(0, str(REPO / "src"))

from rdflib import URIRef, Variable  # noqa: E402
from rdflib.namespace import RDF  # noqa: E402
from rdflib.plugins.sparql.parserutils import CompValue  # noqa: E402

from nl2sparql.guard import PropertyKinds  # noqa: E402
from nl2sparql.guard.provenance import QueryFacts, Sensitivity, analyse  # noqa: E402
from nl2sparql.guard.rules import RuleSettings, _top_projection, _variables_in  # noqa: E402
from nl2sparql.sparql.parse import parse_sparql  # noqa: E402
from nl2sparql.sparql.prefixes import SPHN, add_missing_prefixes  # noqa: E402

kinds = PropertyKinds.load(REPO / "data" / "property_kinds.json")


def _extends(algebra) -> dict:
    found = {}

    def walk(node):
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, CompValue):
            if node.name == "Extend":
                found[node.var] = node.expr
            for key, value in node.items():
                if not key.startswith("_"):
                    walk(value)

    walk(algebra)
    return found


def _short(iri: URIRef) -> str:
    text = str(iri)
    return "sphn:" + text[len(SPHN):] if text.startswith(SPHN) else f"<{text}>"


def explain_group_key(variable: Variable, facts: QueryFacts, extends: dict) -> str:
    """Why grouping by `variable` yields one row per record, and the pattern to use instead."""
    origin = variable
    seen = set()
    while origin in extends and origin not in seen:  # follow BIND(STR(?code) AS ?route) back to ?code
        seen.add(origin)
        expr = extends[origin]
        if isinstance(expr, Variable) and expr in facts.aggregates:  # rdflib: projected group key = SAMPLE(?key)
            break
        sources = [v for v in _variables_in(expr) if facts.sensitivity.get(v, Sensitivity.CONSTANT) >= Sensitivity.INSTANCE]
        if not sources:
            break
        origin = sources[0]
    via = f"?{variable} is built from ?{origin}, which" if origin != variable else f"?{variable}"
    graph_terms = {t.subject for t in facts.triples} | {t.object for t in facts.triples}
    if facts.sensitivity.get(origin) is Sensitivity.IDENTIFIER and origin not in graph_terms:
        return (f"{via} is the patient graph name. Never group by it or return it; count patients with "
                f"COUNT(DISTINCT ?{origin}) and group by a category (a class, a code concept or a value-set entry).")
    for triple in facts.triples:
        if triple.object == origin and isinstance(triple.predicate, URIRef) and triple.predicate != RDF.type:
            predicate = _short(triple.predicate)
            kind = (kinds.kinds.get(str(triple.predicate)) or {}).get("kind")
            if predicate.endswith("Code") or predicate == "sphn:hasCode":
                return (f"{via} is a code node ({predicate}): one node per record, not a category. Group by the "
                        f"code's class instead: ?x {predicate} ?{origin} . ?{origin} a ?concept . GROUP BY ?concept "
                        f"(label it with VALUES (?concept ?label) {{ ... }} and GROUP BY ?label when the question names the categories).")
            if kind == "resource":
                return (f"{via} is a record linked by {predicate}: a unique IRI per record. Group by the class of its code "
                        f"instead: ?{origin} sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept.")
            if facts.sensitivity.get(origin) is Sensitivity.TIMESTAMP:
                return (f"{via} is an exact timestamp ({predicate}): it singles out records. Group by "
                        f"YEAR(?{origin}) or MONTH(?{origin}) instead: BIND(YEAR(?{origin}) AS ?year) ... GROUP BY ?year.")
    for triple in facts.triples:
        if triple.subject == origin and triple.predicate == RDF.type and isinstance(triple.object, URIRef):
            return (f"{via} is a record IRI (a {_short(triple.object)}): one row per record. Do not group by it; "
                    f"count records with COUNT(DISTINCT ?{origin}) and group by a category or a code concept.")
    return (f"{via} is a record IRI: one row per record. Count records with COUNT(DISTINCT ?{origin}) and group by a "
            f"category or a code concept (?x sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept).")


def hints_for(sparql: str) -> list[str]:
    parsed = parse_sparql(add_missing_prefixes(sparql))
    if parsed.kind != "SelectQuery":
        return []
    facts = analyse(parsed.algebra, kinds, RuleSettings().ontology_graphs)
    extends = _extends(parsed.algebra)
    _, _, aggregate_join, group_exprs = _top_projection(parsed.algebra)
    out = []
    for expr in group_exprs:
        if isinstance(expr, Variable):
            level = facts.sensitivity.get(expr, Sensitivity.CONSTANT)
            if level >= Sensitivity.INSTANCE or level is Sensitivity.TIMESTAMP:
                out.append(explain_group_key(expr, facts, extends))
    return out


if __name__ == "__main__":
    here = Path(__file__).parent
    for name in ("pipeline_wrong_verdict__intent-028__generated_sparql.rq", "allowed_request_blocked__q-057__first_try_sparql.rq",
                 "allowed_request_blocked__q-018__first_try_sparql.rq", "allowed_request_blocked__q-030__first_try_sparql.rq",
                 "extra__official-10__first_try_sparql.rq", "extra__official-10__generated_sparql.rq"):
        print("==", name)
        for hint in hints_for((here / name).read_text()):
            print("   ", hint)
