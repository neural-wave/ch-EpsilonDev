"""Prototype judge checks, run on the 13 wrong cases (final + first try) and on the dev references.

Checks:
  U  unbound variable: a variable used in SELECT / GROUP BY / ORDER BY / BIND / FILTER / aggregate that no
     pattern, BIND, VALUES, GRAPH or subquery projection binds in its scope.
  I  OPTION (inference ...) on a type pattern whose class is a variable that is grouped by or projected.
  D  two or more projected columns with the identical aggregate expression.
  L  a variable that is the object of a property linking to an SPHN record class (card target) is used as
     the subject of `?x a ?var` (treated as a code node).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from rdflib import URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.config import REPO_ROOT
from nl2sparql.context.schema import SchemaCards
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import SPHN, add_missing_prefixes

SCHEMA = SchemaCards.load(REPO_ROOT / "data" / "schema_cards.json", REPO_ROOT / "assets" / "value_sets.json")
INFERENCE_VAR_RE = re.compile(r"\?(\w+)\s+(?:a|rdf:type)\s+\?(\w+)\s+OPTION\s*\(\s*inference", re.IGNORECASE)


def _vars(expr):
    if isinstance(expr, Variable):
        yield expr
    elif isinstance(expr, (list, tuple, set)):
        for item in expr:
            yield from _vars(item)
    elif isinstance(expr, CompValue):
        if expr.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
            return
        for key, value in expr.items():
            if not key.startswith("_"):
                yield from _vars(value)


def bound_vars(node) -> set:
    """Variables a pattern can bind, respecting sub-select scope (a subquery exports only its PV)."""
    if isinstance(node, (list, tuple, set)):
        found = set()
        for item in node:
            found |= bound_vars(item)
        return found
    if not isinstance(node, CompValue):
        return set()
    name = node.name
    if name == "BGP":
        return {t for triple in node.triples for t in triple if isinstance(t, Variable)}
    if name == "Graph":
        found = bound_vars(node.p)
        if isinstance(node.term, Variable):
            found.add(node.term)
        return found
    if name == "Extend":
        if isinstance(node.expr, Variable) and str(node.expr).startswith("__agg"):
            return bound_vars(node.p)
        return bound_vars(node.p) | {node.var}
    if name == "values" or name == "ToMultiSet" and isinstance(node.p, CompValue) and node.p.name == "values":
        values = node if name == "values" else node.p
        return set(values.res[0].keys()) if values.res else set()
    if name == "Project":  # a sub-select exports only what it projects
        return set(node.PV)
    if name in ("SelectQuery",):
        return bound_vars(node.p)
    found = set()
    for key, value in node.items():
        if key.startswith("_") or key in ("expr", "PV", "A", "term", "var"):
            continue
        found |= bound_vars(value)
    return found


def used_vars_outside_patterns(node) -> list[tuple[str, Variable]]:
    """(where, variable) for variables used in expressions: Extend exprs, Filter exprs, Group exprs, aggregates, PV."""
    found = []
    if isinstance(node, (list, tuple, set)):
        for item in node:
            found += used_vars_outside_patterns(item)
        return found
    if not isinstance(node, CompValue):
        return found
    name = node.name
    if name == "Project":
        return found
    if name == "Extend":
        found += [("BIND", v) for v in _vars(node.expr)]
    elif name == "Filter":
        found += [("FILTER/HAVING", v) for v in _vars(node.expr)]
    elif name == "Group":
        found += [("GROUP BY", v) for v in _vars(node.expr)]
    elif name == "OrderBy":
        found += [("ORDER BY", v) for v in _vars(node.expr)]
    elif name == "AggregateJoin":
        for agg in node.A:
            found += [("aggregate", v) for v in _vars(agg.vars)]
    for key, value in node.items():
        if not key.startswith("_"):
            found += used_vars_outside_patterns(value)
    return found


def check_unbound(algebra) -> list[str]:
    """Walk each Project scope: variables used in expressions must be bound in that scope.
    Aggregate outputs count as bound only when every input variable is bound."""
    errors = []

    def aggregates_in(node, found):
        if isinstance(node, (list, tuple, set)):
            for item in node:
                aggregates_in(item, found)
            return
        if not isinstance(node, CompValue) or node.name == "Project":
            return
        if node.name == "AggregateJoin":
            for agg in node.A:
                found[agg.res] = (agg.name, [] if agg.vars == "*" else list(_vars(agg.vars)))
        for key, value in node.items():
            if not key.startswith("_"):
                aggregates_in(value, found)

    def extends_in(node, found):
        if isinstance(node, (list, tuple, set)):
            for item in node:
                extends_in(item, found)
            return
        if not isinstance(node, CompValue) or node.name == "Project":
            return
        if node.name == "Extend":
            found.append((node.var, node.expr))
        for key, value in node.items():
            if not key.startswith("_"):
                extends_in(value, found)

    def visit_scope(project):
        inner = project.p
        bound = bound_vars(inner)
        aggs = {}
        aggregates_in(inner, aggs)
        extends = []
        extends_in(inner, extends)
        for var, expr in extends:  # an alias of an aggregate is defined, unless it is rdflib's SAMPLE of a group key
            if isinstance(expr, Variable) and str(expr).startswith("__agg"):
                name, inputs = aggs.get(expr, ("", []))
                if name == "Aggregate_Sample" and inputs == [var]:
                    continue
                bound.add(var)
        used = used_vars_outside_patterns(inner)
        used += [("SELECT", v) for v in project.PV]
        for where, v in used:
            if str(v).startswith("__agg"):
                for inp in aggs.get(v, ("", []))[1]:
                    if inp not in bound:
                        errors.append(f"?{inp} ({where}: inside an aggregate) is never bound")
                continue
            if v not in bound and v not in aggs:
                errors.append(f"?{v} ({where}) is never bound")

        def find_projects(node):
            if isinstance(node, (list, tuple, set)):
                for item in node:
                    find_projects(item)
                return
            if not isinstance(node, CompValue):
                return
            if node.name == "Project":
                visit_scope(node)
                return
            for key, value in node.items():
                if not key.startswith("_"):
                    find_projects(value)
        find_projects(inner)

    top = algebra.p
    while isinstance(top, CompValue) and top.name in ("Slice", "Distinct", "Reduced"):
        top = top.p
    if isinstance(top, CompValue) and top.name == "Project":
        visit_scope(top)
    return list(dict.fromkeys(errors))


def check_inference_on_variable(text: str, algebra) -> list[str]:
    errors = []
    grouped = set()
    projected = set()

    def visit(node):
        if isinstance(node, (list, tuple, set)):
            for item in node:
                visit(item)
            return
        if not isinstance(node, CompValue):
            return
        if node.name == "Group" and node.expr:
            grouped.update(v for v in _vars(node.expr))
        if node.name == "Project":
            projected.update(node.PV)
        for key, value in node.items():
            if not key.startswith("_"):
                visit(value)

    visit(algebra)
    for subject, cls in INFERENCE_VAR_RE.findall(text):
        v = Variable(cls)
        if v in grouped or v in projected:
            errors.append(f"OPTION (inference) on ?{subject} a ?{cls} adds every superclass of the code as an extra ?{cls} row")
    return errors


def check_duplicate_aggregates(algebra) -> list[str]:
    node = algebra.p
    while isinstance(node, CompValue) and node.name in ("Slice", "Distinct", "Reduced"):
        node = node.p
    if not isinstance(node, CompValue) or node.name != "Project":
        return []
    extends = {}
    current = node.p
    while isinstance(current, CompValue) and current.name in ("OrderBy", "Filter", "Extend", "Distinct", "Slice", "ToMultiSet"):
        if current.name == "Extend":
            extends[current.var] = current.expr
        current = current.p
    if not isinstance(current, CompValue) or current.name != "AggregateJoin":
        return []
    agg_repr = {}
    for agg in current.A:
        agg_repr[agg.res] = f"{agg.name}({'DISTINCT ' if agg.distinct else ''}{agg.vars})"
    seen = {}
    for var in node.PV:
        expr = extends.get(var)
        if isinstance(expr, Variable) and expr in agg_repr:
            key = agg_repr[expr]
            seen.setdefault(key, []).append(var)
    return [f"columns {', '.join('?' + str(v) for v in vs)} are all {k}" for k, vs in seen.items() if len(vs) > 1]


def check_record_link_as_code(algebra) -> list[str]:
    """?x <prop> ?y . ?y a ?cls  where prop's card target is an SPHN record class (not Code)."""
    link_targets = {}
    for cname, card in SCHEMA.classes.items():
        for prop, info in card["properties"].items():
            target = info.get("target")
            if target and target not in ("Code", "Terminology") and not prop.endswith("Code"):
                link_targets.setdefault(prop, set()).add(target)
    triples = []
    values_vars = set()

    def visit(node):
        if isinstance(node, (list, tuple, set)):
            for item in node:
                visit(item)
            return
        if not isinstance(node, CompValue):
            return
        if node.name == "BGP":
            triples.extend(node.triples)
        if node.name == "values" and node.res:
            values_vars.update(node.res[0].keys())
        for key, value in node.items():
            if not key.startswith("_"):
                visit(value)

    visit(algebra)
    linked = {}
    for s, p, o in triples:
        if isinstance(p, URIRef) and str(p).startswith(SPHN) and isinstance(o, Variable):
            prop = str(p)[len(SPHN):]
            if prop in link_targets:
                linked[o] = (prop, link_targets[prop])
    errors = []
    for s, p, o in triples:
        if p == RDF.type and isinstance(s, Variable) and isinstance(o, Variable) and s in linked and o not in values_vars:
            prop, targets = linked[s]
            errors.append(f"?{s} is a sphn:{'/'.join(sorted(targets))} record (object of {prop}), not a code node: "
                          f"?{s} a ?{o} only yields that class; use the record's own properties")
    return errors


def run_all(text: str) -> dict:
    fixed = add_missing_prefixes(text.strip() + "\n")
    parsed = parse_sparql(fixed)
    if parsed.kind != "SelectQuery":
        return {"parse": parsed.error or parsed.kind}
    return {
        "U": check_unbound(parsed.algebra),
        "I": check_inference_on_variable(fixed, parsed.algebra),
        "D": check_duplicate_aggregates(parsed.algebra),
        "L": check_record_link_as_code(parsed.algebra),
    }


def main():
    base = Path(sys.argv[1])
    ids = sys.argv[2:]
    print("== wrong cases (final / first try)")
    for i in ids:
        it = json.load(open(base / i / "item.json"))
        for label, text in (("final", it["generated_sparql"]), ("first", it["first_try_sparql"])):
            if not text:
                continue
            res = run_all(text)
            hits = {k: v for k, v in res.items() if v}
            print(f"{i:12} {label:5} {json.dumps(hits) if hits else '-'}")
    print("== dev references + compliant examples (false-positive test)")
    fp = 0
    n = 0
    rows = []
    for line in (REPO_ROOT / "data" / "examples.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r["compliant"]:
                rows.append((r["id"], r["sparql"]))
    for line in (REPO_ROOT / "eval" / "query_questions.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("split") == "holdout":
                continue
            rows.append((r["id"], r["reference_sparql"]))
    for rid, sparql in rows:
        n += 1
        res = run_all(sparql)
        hits = {k: v for k, v in res.items() if v}
        if hits:
            fp += 1
            print(f"  FP? {rid:12} {json.dumps(hits)}")
    print(f"references checked: {n}, with hits: {fp}")


if __name__ == "__main__":
    main()
