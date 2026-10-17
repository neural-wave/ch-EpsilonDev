"""Proposed R1 extension: any expression that pins an identifier-level variable (patient graph name,
identifier literal, STR(?g), hashes of them, ...) to something that carries no identifier.

Closes the bypasses of the current `_identifier_comparisons`, which only looks at RelationalExpression
with Literal/URIRef constants and at string matchers with a Literal pattern:
  - FILTER(STRSTARTS(STR(?g), ?prefix)) with VALUES ?prefix { "...1050" }   (pattern is a Variable)
  - FILTER(STRSTARTS(STR(?g), CONCAT("gen_mock_", "data-1_1050")))         (pattern is an expression)
  - FILTER(?g = IRI("http://example.org/graph/gen_mock_data-1_1050123"))     (other side is IRI(...))
  - FILTER(STR(?g) = CONCAT(...))
  - FILTER(bif:starts_with(STR(?g), "...1050"))                              (custom function)
  - BIND(IF(STRSTARTS(STR(?g), "...1050"), 1, 0) AS ?flag) ... SUM(?flag)    (comparison inside BIND)
  - SUM(IF(STRSTARTS(STR(?g), "...1050"), 1, 0))                              (comparison inside an aggregate)
  - FILTER(STRLEN(STR(?g)) = 45)                                              (non-identity function of ?g)
  - VALUES ?g { <urn:not-the-patient-prefix> }                                (VALUES on the graph variable)
  - BIND(IRI("...") AS ?g) / BIND("..." AS ?g)                                (constant into the graph variable)

Kept: the bare-prefix exception for string matchers whose pattern is a digit-free literal
(FILTER(STRSTARTS(STR(?g), "http://example.org/graph/")) stays allowed), BIND(?g AS ?x),
COALESCE(?a, ?b), IF(BOUND(?e), ?g, 0), ?g != ?g2, BOUND(?g), COUNT(DISTINCT ?g), HAVING on counts.

This file does not modify the repo: it wraps nl2sparql.guard.rules.check_rules.
"""
from __future__ import annotations

from rdflib import Literal, URIRef, Variable
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.guard import rules
from nl2sparql.guard.provenance import QueryFacts, Sensitivity, analyse, expression_sensitivity
from nl2sparql.guard.rules import RuleSettings, Violation
from nl2sparql.sparql.parse import ParsedQuery

COMPARISONS = {"RelationalExpression"}
STRING_MATCHERS = rules.STRING_MATCHERS  # REGEX, CONTAINS, STRSTARTS, STRENDS, sameTerm
IDENTITY_SELECTORS = {"Builtin_IF", "Builtin_COALESCE", "Builtin_BOUND"}  # never compare, only pick
WHERE_NAMES = {"Filter": "FILTER", "Extend": "BIND", "OrderCondition": "ORDER BY", "Group": "GROUP BY"}


def _identifier_variables(expr, facts: QueryFacts) -> set[Variable]:
    """Every variable inside expr that can hold an identifier (patient graph, identifier literal, ...)."""
    found: set[Variable] = set()

    def walk(node) -> None:
        if isinstance(node, Variable):
            # __agg_N__ names are overwritten across scopes (GUARD.md section 7): leave aggregate results
            # to the existing _identifier_comparisons and the projection rules.
            if not str(node).startswith("__agg_") and facts.sensitivity.get(node, Sensitivity.CONSTANT) is Sensitivity.IDENTIFIER:
                found.add(node)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, CompValue):
            if node.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
                return
            for key, value in node.items():
                if not key.startswith("_"):
                    walk(value)

    walk(expr)
    return found


def _carries_no_identifier(expr, facts: QueryFacts) -> bool:
    """True when expr is constant-like: no variable that holds a record IRI or an identifier."""
    if isinstance(expr, Variable):  # before the str check: rdflib terms are str subclasses
        if str(expr).startswith("__agg_"):
            return False  # an aggregate result: its recorded level may belong to another scope, never treat as constant
        return facts.sensitivity.get(expr, Sensitivity.CONSTANT) < Sensitivity.INSTANCE
    if isinstance(expr, (Literal, URIRef, str)) or expr is None:
        return True
    if isinstance(expr, (list, tuple)):
        return all(_carries_no_identifier(item, facts) for item in expr)
    if isinstance(expr, CompValue):
        if expr.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
            return True
        return all(_carries_no_identifier(value, facts) for key, value in expr.items() if not key.startswith("_"))
    return True


def _bare_prefix(node: CompValue) -> bool:
    """STRSTARTS/CONTAINS/REGEX/... whose pattern is a digit-free literal: the house-style graph prefix."""
    pattern = node.get("arg2") if "arg2" in node else node.get("pattern")
    return isinstance(pattern, Literal) and not any(ch.isdigit() for ch in str(pattern))


def _operands(node: CompValue) -> list:
    if node.name == "RelationalExpression":
        other = node.other if isinstance(node.other, list) else [node.other]
        return [node.expr, *other]
    if node.name == "Builtin_REGEX":
        return [node.text, node.pattern]
    if node.name in STRING_MATCHERS:
        return [node.arg1, node.arg2]
    if node.name == "Function":  # custom functions such as bif:starts_with(...)
        return list(node.expr or [])
    return []


def _pinned_identifiers(expr, facts: QueryFacts):
    """Yield (identifier variables, kind) for every comparison that pins an identifier to a non-identifier."""
    if isinstance(expr, (list, tuple)):
        for item in expr:
            yield from _pinned_identifiers(item, facts)
        return
    if not isinstance(expr, CompValue):
        return
    if expr.name in COMPARISONS or expr.name in STRING_MATCHERS or expr.name == "Function":
        operands = _operands(expr)
        bare = expr.name == "RelationalExpression" and expr.other is None  # rdflib wraps plain expressions too
        if operands and not bare:
            identifier_sides = [o for o in operands if _identifier_variables(o, facts)]
            other_sides = [o for o in operands if not _identifier_variables(o, facts)]
            if identifier_sides and other_sides and all(_carries_no_identifier(o, facts) for o in other_sides):
                if not (expr.name in STRING_MATCHERS and _bare_prefix(expr)):
                    variables = set().union(*(_identifier_variables(o, facts) for o in identifier_sides))
                    yield variables, expr.name
    for key, value in expr.items():
        if not key.startswith("_"):
            yield from _pinned_identifiers(value, facts)


def _expressions(algebra):
    """Every expression in the query with where it sits: FILTER, BIND, ORDER BY, GROUP BY, aggregates."""
    if isinstance(algebra, (list, tuple)):
        for item in algebra:
            yield from _expressions(item)
        return
    if not isinstance(algebra, CompValue):
        return
    if algebra.name == "Filter":
        yield algebra.expr, "FILTER"
    elif algebra.name == "Extend":
        yield algebra.expr, "BIND"
    elif algebra.name == "OrderBy":
        for condition in algebra.expr or []:
            yield condition.expr, "ORDER BY"
    elif algebra.name == "Group" and algebra.expr:
        yield list(algebra.expr), "GROUP BY"
    elif algebra.name == "AggregateJoin":
        for aggregate in algebra.A:
            yield aggregate.vars, "an aggregate"
    for key, value in algebra.items():
        if not key.startswith("_") and key != "PV":
            yield from _expressions(value)


def _values_nodes(algebra):
    if isinstance(algebra, (list, tuple)):
        for item in algebra:
            yield from _values_nodes(item)
    elif isinstance(algebra, CompValue):
        if algebra.name == "values":
            yield algebra
        for key, value in algebra.items():
            if not key.startswith("_"):
                yield from _values_nodes(value)


def _extend_nodes(algebra):
    if isinstance(algebra, (list, tuple)):
        for item in algebra:
            yield from _extend_nodes(item)
    elif isinstance(algebra, CompValue):
        if algebra.name == "Extend":
            yield algebra
        for key, value in algebra.items():
            if not key.startswith("_"):
                yield from _extend_nodes(value)


def _graph_variables(algebra) -> set[Variable]:
    """Variables used as GRAPH ?g terms."""
    found: set[Variable] = set()

    def walk(node) -> None:
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, CompValue):
            if node.name == "Graph" and isinstance(node.term, Variable):
                found.add(node.term)
            for key, value in node.items():
                if not key.startswith("_"):
                    walk(value)

    walk(algebra)
    return found


def identifier_constraints(algebra, facts: QueryFacts) -> list[Violation]:
    """R1: no expression may pin a patient graph name or an identifier to constants or derived values."""
    found: list[Violation] = []
    graph_variables = _graph_variables(algebra)
    for expr, where in _expressions(algebra):
        for variables, kind in _pinned_identifiers(expr, facts):
            names = ", ".join(sorted(f"?{v}" for v in variables))
            found.append(Violation(
                "R1",
                f"The {where} restricts the patient graph name or identifier {names} to specific values. "
                "Identifiers and graph names may only be counted (COUNT(DISTINCT ?g)), never compared, "
                "matched or filtered.",
            ))
    for node in _values_nodes(algebra):
        for row in node.res[:1]:
            for variable in row:
                if facts.sensitivity.get(variable) is Sensitivity.IDENTIFIER:
                    found.append(Violation("R1", f"VALUES pins the patient graph name or identifier ?{variable} to fixed values."))
    for node in _extend_nodes(algebra):
        # Only variables that name a graph: BIND(IRI("...") AS ?g) before GRAPH ?g. A constant bound into a
        # renamed patient variable in one UNION branch (BIND(FALSE AS ?early_birth)) is odd, not a leak.
        # A plain variable on the right (rdflib's Extend(?g = __agg_N__) for a projected GROUP BY ?g) is never a leak.
        if node.var in graph_variables and not isinstance(node.expr, Variable) and _carries_no_identifier(node.expr, facts):
            found.append(Violation("R1", f"BIND assigns a fixed value to the patient graph name ?{node.var}."))
    return found


def check_rules_patched(parsed: ParsedQuery, kinds, settings: RuleSettings = RuleSettings()) -> list[Violation]:
    violations = rules.check_rules(parsed, kinds, settings)
    if parsed.kind != "SelectQuery":
        return violations
    facts = analyse(parsed.algebra, kinds, settings.ontology_graphs)
    extra = identifier_constraints(parsed.algebra, facts)
    return list(dict.fromkeys(violations + extra))
