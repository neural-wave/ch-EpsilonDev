"""The five compliance rules from Query_Compliance.md, checked on the parsed query."""
from __future__ import annotations

import re
from dataclasses import dataclass

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.guard.provenance import PropertyKinds, QueryFacts, Sensitivity, analyse, expression_sensitivity
from nl2sparql.sparql.parse import ParsedQuery

RECORD_IRI_RE = re.compile(r"^https://biomedit\.ch/rdf/sphn-resource/[^/]*-sphn-")
SELECT_STAR_RE = re.compile(r"\bSELECT\s+(?:DISTINCT\s+|REDUCED\s+)?\*", re.IGNORECASE)
FIRST_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
STRING_MATCHERS = {"Builtin_REGEX", "Builtin_CONTAINS", "Builtin_STRSTARTS", "Builtin_STRENDS", "Builtin_sameTerm"}
LISTING_AGGREGATES = {"Aggregate_Sample", "Aggregate_GroupConcat"}
PICKING_AGGREGATES = {"Aggregate_Min", "Aggregate_Max", "Aggregate_Sample", "Aggregate_GroupConcat"}
AGGREGATE_NAMES = {
    "Aggregate_Min": "MIN", "Aggregate_Max": "MAX", "Aggregate_Sample": "SAMPLE",
    "Aggregate_GroupConcat": "GROUP_CONCAT",
}
QUERY_FORMS = {"ConstructQuery": "CONSTRUCT", "AskQuery": "ASK", "DescribeQuery": "DESCRIBE"}


@dataclass(frozen=True)
class Violation:
    rule: str  # R1..R5, or PARSE when the query cannot be analysed
    message: str


@dataclass(frozen=True)
class RuleSettings:
    patient_graph_prefix: str = "http://example.org/graph/"
    ontology_graphs: frozenset[str] = frozenset({"https://biomedit.ch/rdf/sphn-resource/global"})


def check_rules(parsed: ParsedQuery, kinds: PropertyKinds, settings: RuleSettings = RuleSettings()) -> list[Violation]:
    if parsed.kind == "Invalid":
        return [Violation("PARSE", f"The query could not be parsed: {parsed.error}")]
    if parsed.kind == "Update":
        return [Violation("R4", "The query modifies data. Only read-only SELECT queries are allowed.")]
    if parsed.kind != "SelectQuery":
        form = QUERY_FORMS.get(parsed.kind, parsed.kind)
        return [Violation("R4", f"The query is a {form} query. Only SELECT queries are allowed.")]
    facts = analyse(parsed.algebra, kinds, settings.ontology_graphs)
    violations: list[Violation] = []
    violations += _single_patient(facts, settings)
    violations += _whole_graph(facts)
    if facts.touches_patient_data:
        violations += _projection(parsed, facts)
    return list(dict.fromkeys(violations))  # drop duplicates, keep order


def _single_patient(facts: QueryFacts, settings: RuleSettings) -> list[Violation]:
    found = []
    for iri in sorted(facts.iris):
        text = str(iri)
        if text.startswith(settings.patient_graph_prefix):
            found.append(Violation("R1", f"The query targets a single patient graph <{text}>."))
        elif RECORD_IRI_RE.match(text):
            found.append(Violation("R1", f"The query targets a single patient record <{text}>."))
    for triple in facts.triples:
        if isinstance(triple.object, Literal) and isinstance(triple.predicate, URIRef):
            if PropertyKinds().object_sensitivity(triple.predicate) is Sensitivity.IDENTIFIER:
                found.append(Violation("R1", f'The query looks up the identifier "{triple.object}".'))
    for expr in facts.filters:
        for value in _identifier_comparisons(expr, facts):
            found.append(Violation("R1", f"The query filters on the identifier value {value}."))
    return found


def _identifier_comparisons(expr, facts: QueryFacts):
    if isinstance(expr, (list, tuple)):
        for item in expr:
            yield from _identifier_comparisons(item, facts)
        return
    if not isinstance(expr, CompValue):
        return
    if expr.name == "RelationalExpression" and expr.other is not None:
        sides = [(expr.expr, expr.other), (expr.other, expr.expr)]
        for left, right in sides:
            constants = right if isinstance(right, list) else [right]
            if expression_sensitivity(left, facts) is Sensitivity.IDENTIFIER and constants and all(
                isinstance(c, (Literal, URIRef)) for c in constants
            ):
                yield ", ".join(f'"{c}"' for c in constants)
    if expr.name in STRING_MATCHERS:
        target, pattern = expr.get("arg1", expr.get("text")), expr.get("arg2", expr.get("pattern"))
        if (
            expression_sensitivity(target, facts) is Sensitivity.IDENTIFIER
            and isinstance(pattern, Literal)
            and any(ch.isdigit() for ch in str(pattern))
        ):
            yield f'"{pattern}"'
    for key, value in expr.items():
        if not key.startswith("_"):
            yield from _identifier_comparisons(value, facts)


def _whole_graph(facts: QueryFacts) -> list[Violation]:
    patient_triples = [t for t in facts.triples if not t.in_ontology_graph]
    if not patient_triples:
        return []
    for triple in patient_triples:
        if isinstance(triple.predicate, Variable):
            continue
        if triple.predicate == RDF.type and isinstance(triple.object, Variable):
            continue
        return []  # at least one pattern restricts by a concrete property or class
    return [Violation("R3", "The query matches the whole graph without restricting it to a concept.")]


def _projection(parsed: ParsedQuery, facts: QueryFacts) -> list[Violation]:
    first_select = FIRST_SELECT_RE.search(parsed.text)
    if first_select and SELECT_STAR_RE.match(parsed.text, first_select.start()):
        return [Violation("R2", "SELECT * returns raw rows. Only aggregated results are allowed.")]
    projected, extends, aggregate_join, group_exprs = _top_projection(parsed.algebra)
    if aggregate_join is None:
        names = ", ".join(f"?{v}" for v in projected)
        return [Violation("R2", f"The query returns individual rows ({names}) instead of aggregated results.")]
    group_vars = {expr for expr in group_exprs if isinstance(expr, Variable)}
    found: list[Violation] = []
    for expr in group_exprs:
        level = expression_sensitivity(expr, facts)
        label = f"?{expr}" if isinstance(expr, Variable) else "an expression"
        if level >= Sensitivity.INSTANCE:
            rule = "R5" if expr in projected else "R2"
            found.append(Violation(rule, f"Grouping by {label} returns one row per patient or record."))
        elif level is Sensitivity.TIMESTAMP:
            found.append(Violation("R2", f"Grouping by the exact timestamp {label} singles out individual records."))
    for variable in projected:
        expr = extends.get(variable)
        if expr is None:
            if variable not in group_vars:
                found.append(Violation("R2", f"?{variable} is returned per row without aggregation."))
            continue
        found += _expression_violations(variable, expr, facts, group_vars)
    return found


def _expression_violations(variable, expr, facts: QueryFacts, group_vars: set) -> list[Violation]:
    found = []
    for used in _variables_in(expr):
        aggregate = facts.aggregates.get(used)
        if aggregate is not None:
            if aggregate.name not in PICKING_AGGREGATES:
                continue
            if aggregate.name == "Aggregate_Sample" and aggregate.vars in group_vars:
                continue  # rdflib carries projected GROUP BY keys as SAMPLE; the key check covers them
            level = expression_sensitivity(aggregate.vars, facts)
            name = AGGREGATE_NAMES[aggregate.name]
            if level >= Sensitivity.INSTANCE:
                found.append(Violation("R5", f"?{variable} uses {name} over identifiers or record IRIs."))
            elif aggregate.name in LISTING_AGGREGATES and level in (Sensitivity.VALUE, Sensitivity.TIMESTAMP):
                found.append(Violation("R2", f"?{variable} uses {name} to list individual values."))
        elif used not in group_vars:
            found.append(Violation("R2", f"?{variable} uses ?{used} outside an aggregate."))
    return found


def _variables_in(expr):
    if isinstance(expr, Variable):
        yield expr
    elif isinstance(expr, (list, tuple)):
        for item in expr:
            yield from _variables_in(item)
    elif isinstance(expr, CompValue):
        if expr.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
            return
        for key, value in expr.items():
            if not key.startswith("_"):
                yield from _variables_in(value)


def _top_projection(algebra):
    node = algebra.p
    while isinstance(node, CompValue) and node.name in ("Slice", "Distinct", "Reduced"):
        node = node.p
    if not isinstance(node, CompValue) or node.name != "Project":
        return [], {}, None, []
    projected = list(node.PV)
    extends = {}
    current = node.p
    while isinstance(current, CompValue) and current.name in ("OrderBy", "Filter", "Extend", "Distinct", "Slice", "ToMultiSet"):
        if current.name == "Extend":
            extends[current.var] = current.expr
        current = current.p
    if isinstance(current, CompValue) and current.name == "AggregateJoin":
        group = current.p
        group_exprs = list(group.expr) if group.expr else []
        return projected, extends, current, group_exprs
    return projected, extends, None, []
