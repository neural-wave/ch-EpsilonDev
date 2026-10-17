"""The five compliance rules from Query_Compliance.md, checked on the parsed query."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.guard.provenance import PropertyKinds, QueryFacts, Sensitivity, analyse, expression_sensitivity
from nl2sparql.sparql.parse import ParsedQuery

RECORD_IRI_RE = re.compile(r"^https://biomedit\.ch/rdf/sphn-resource/[^/]*-sphn-")
SELECT_STAR_RE = re.compile(r"\bSELECT\s+(?:DISTINCT\s+|REDUCED\s+)?\*", re.IGNORECASE)
FIRST_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
STRING_MATCHERS = {"Builtin_REGEX", "Builtin_CONTAINS", "Builtin_STRSTARTS", "Builtin_STRENDS", "Builtin_sameTerm"}
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
    if facts.services:
        violations.append(
            Violation(
                "R4",
                "The query uses SERVICE to access another endpoint. Only local SELECT queries are allowed.",
            )
        )
    violations += _single_patient(facts, settings)
    violations += _whole_graph(facts)
    if facts.touches_patient_data:
        violations += _minimum_group_size(parsed.algebra, facts)
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
        # CompValue.get takes no default, so look the keys up explicitly: REGEX uses text/pattern.
        target = expr.get("arg1") if "arg1" in expr else expr.get("text")
        pattern = expr.get("arg2") if "arg2" in expr else expr.get("pattern")
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


def _minimum_group_size(algebra, facts: QueryFacts) -> list[Violation]:
    """Reject HAVING clauses that keep groups containing exactly one patient.

    rdflib represents a HAVING clause as a Filter above an AggregateJoin. The
    aggregate result is an internal variable such as ``__agg_2__``; the
    aggregate map built by ``analyse`` lets us identify the original
    ``COUNT(DISTINCT ?g)`` safely without inspecting the query text.
    """
    node = algebra.p
    while isinstance(node, CompValue):
        if node.name == "Filter":
            if _is_single_patient_count(node.expr, facts):
                return [
                    Violation(
                        "R2",
                        "HAVING keeps groups containing exactly one patient. Use a minimum group size greater than one.",
                    )
                ]
            node = node.p
            continue
        if node.name in ("Project", "OrderBy", "Extend", "Distinct", "Reduced", "Slice", "ToMultiSet"):
            node = node.p
            continue
        break
    return []


FLIPPED_OPERATORS = {"=": "=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


def _is_single_patient_count(expr, facts: QueryFacts) -> bool:
    """True when the condition keeps only groups whose count is 1, e.g. = 1, < 2, <= 1, IN (1).

    A conjunction keeps only such groups as soon as one of its parts does.
    """
    if not isinstance(expr, CompValue):
        return False
    if expr.name == "ConditionalAndExpression":
        return any(_is_single_patient_count(part, facts) for part in [expr.expr, *(expr.other or [])])
    if expr.name != "RelationalExpression" or expr.other is None:
        return False
    if expr.op in ("IN", "NOT IN"):
        if expr.op == "IN" and _counts_one_patient(expr.expr, facts):
            kept = {_number(value) for value in expr.other}
            return None not in kept and {count for count in kept if count >= 1} == {1}
        return False
    for counted, constant, op in ((expr.expr, expr.other, expr.op), (expr.other, expr.expr, FLIPPED_OPERATORS.get(expr.op))):
        limit = _number(constant)
        if op is None or limit is None or not _counts_one_patient(counted, facts):
            continue
        if (op == "=" and limit == 1) or (op == "<" and 1 < limit <= 2) or (op == "<=" and 1 <= limit < 2):
            return True
    return False


def _counts_one_patient(variable, facts: QueryFacts) -> bool:
    """A count of 1 here means one patient: a row count, or a distinct count of patients or records."""
    aggregate = facts.aggregates.get(variable) if isinstance(variable, Variable) else None
    if aggregate is None or aggregate.name != "Aggregate_Count":
        return False
    if aggregate.distinct not in (True, "DISTINCT"):
        return True  # COUNT(*) or COUNT(?x) = 1 means a single matching row
    return isinstance(aggregate.vars, Variable) and expression_sensitivity(aggregate.vars, facts) >= Sensitivity.INSTANCE


def _number(value) -> float | None:
    if not isinstance(value, Literal):
        return None
    number = value.toPython()
    if isinstance(number, bool) or not isinstance(number, (int, float, Decimal)):
        return None
    return float(number)


def _projection(parsed: ParsedQuery, facts: QueryFacts) -> list[Violation]:
    first_select = FIRST_SELECT_RE.search(parsed.text)
    if first_select and SELECT_STAR_RE.match(parsed.text, first_select.start()):
        return [Violation("R2", "SELECT * returns raw rows. Only aggregated results are allowed.")]
    projected, extends, aggregate_join, group_exprs = _top_projection(parsed.algebra)
    if aggregate_join is None:
        names = ", ".join(f"?{v}" for v in projected)
        return [Violation("R2", f"The query returns individual rows ({names}) instead of aggregated results.")]
    group_vars = {expr for expr in group_exprs if isinstance(expr, Variable)}
    exposed = _exposed_group_keys(projected, extends, facts, group_vars)
    found: list[Violation] = []
    for expr in group_exprs:
        level = expression_sensitivity(expr, facts)
        label = f"?{expr}" if isinstance(expr, Variable) else "an expression"
        if level >= Sensitivity.INSTANCE:
            rule = "R5" if expr in exposed else "R2"
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


def _exposed_group_keys(projected, extends, facts: QueryFacts, group_vars: set) -> set:
    """Group keys whose values reach the output, directly or wrapped (e.g. STR(?g))."""
    exposed = {variable for variable in projected if variable in group_vars}
    for variable in projected:
        for used in _variables_in(extends.get(variable)):
            aggregate = facts.aggregates.get(used)
            if aggregate is not None and aggregate.name == "Aggregate_Sample" and aggregate.vars in group_vars:
                exposed.add(aggregate.vars)
    return exposed


def _expression_violations(variable, expr, facts: QueryFacts, group_vars: set) -> list[Violation]:
    found = []
    # What reaches the output: YEAR(SAMPLE(?date)) only exposes a year, SAMPLE(?date) the exact date.
    output = expression_sensitivity(expr, facts)
    for used in _variables_in(expr):
        aggregate = facts.aggregates.get(used)
        if aggregate is None:
            if used not in group_vars:
                found.append(Violation("R2", f"?{variable} uses ?{used} outside an aggregate."))
            continue
        if aggregate.name not in PICKING_AGGREGATES:
            continue
        if aggregate.name == "Aggregate_Sample" and aggregate.vars in group_vars:
            continue  # rdflib carries projected GROUP BY keys as SAMPLE; the key check covers them
        name = AGGREGATE_NAMES[aggregate.name]
        if output >= Sensitivity.INSTANCE:
            found.append(Violation("R5", f"?{variable} uses {name} over identifiers or record IRIs."))
        elif aggregate.name == "Aggregate_GroupConcat" and output >= Sensitivity.VALUE:
            found.append(Violation("R2", f"?{variable} uses {name} to list individual values."))
        elif aggregate.name == "Aggregate_Sample" and output >= Sensitivity.TIMESTAMP:
            found.append(Violation("R2", f"?{variable} uses {name} to pick one record's exact value."))
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
