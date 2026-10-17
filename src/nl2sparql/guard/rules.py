"""The five compliance rules from Query_Compliance.md, checked on the parsed query."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.paths import InvPath
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.guard.provenance import (
    EXISTS, SPHN, PropertyKinds, QueryFacts, Sensitivity, analyse, exists_pattern, expression_sensitivity,
)
from nl2sparql.sparql.parse import ParsedQuery

RECORD_IRI_RE = re.compile(r"^https://biomedit\.ch/rdf/sphn-resource/[^/]*-sphn-")
SELECT_STAR_RE = re.compile(r"\bSELECT\s+(?:DISTINCT\s+|REDUCED\s+)?\*", re.IGNORECASE)
FIRST_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
STRING_MATCHERS = {"Builtin_REGEX", "Builtin_CONTAINS", "Builtin_STRSTARTS", "Builtin_STRENDS", "Builtin_sameTerm"}
CUSTOM_FUNCTION = "Function"  # bif:starts_with(...) and any other extension function
PICKING_AGGREGATES = {"Aggregate_Min", "Aggregate_Max", "Aggregate_Sample", "Aggregate_GroupConcat"}
AGGREGATE_NAMES = {
    "Aggregate_Min": "MIN", "Aggregate_Max": "MAX", "Aggregate_Sample": "SAMPLE",
    "Aggregate_GroupConcat": "GROUP_CONCAT",
}
QUERY_FORMS = {"ConstructQuery": "CONSTRUCT", "AskQuery": "ASK", "DescribeQuery": "DESCRIBE"}
CATEGORY_ADVICE = "group by a category (a class, a code concept or a value-set entry)"


@dataclass(frozen=True)
class Violation:
    rule: str  # R1..R5, or PARSE when the query cannot be analysed
    message: str
    grouping: bool = False  # a GROUP BY key singles out patients or records; the retry hint says what to group by


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
    violations += _single_patient(facts, kinds, settings)
    violations += _identifier_constraints(parsed.algebra, facts)
    violations += _whole_graph(facts)
    if facts.touches_patient_data:
        violations += _minimum_group_size(parsed.algebra, facts)
        violations += _projection(parsed, facts, kinds)
    return list(dict.fromkeys(violations))  # drop duplicates, keep order


def _single_patient(
    facts: QueryFacts, kinds: PropertyKinds, settings: RuleSettings
) -> list[Violation]:
    found = []
    for iri in sorted(facts.iris):
        text = str(iri)
        if text.startswith(settings.patient_graph_prefix):
            found.append(Violation("R1", f"The query targets a single patient graph <{text}>."))
        elif RECORD_IRI_RE.match(text):
            found.append(Violation("R1", f"The query targets a single patient record <{text}>."))
    for triple in facts.triples:
        if isinstance(triple.object, Literal) and isinstance(triple.predicate, URIRef):
            if kinds.object_sensitivity(triple.predicate) is Sensitivity.IDENTIFIER:
                found.append(Violation("R1", f'The query looks up the identifier "{triple.object}".'))
        elif isinstance(triple.subject, Literal) and _inverse_identifier_path(triple.predicate, kinds):
            found.append(Violation("R1", f'The query looks up the identifier "{triple.subject}".'))
    return found


def _inverse_identifier_path(predicate: object, kinds: PropertyKinds) -> bool:
    """Return whether an inverse path traverses an identifier-valued predicate."""
    inverted = False
    while isinstance(predicate, InvPath):
        inverted = not inverted
        predicate = predicate.arg
    return (
        inverted
        and isinstance(predicate, URIRef)
        and kinds.object_sensitivity(predicate) is Sensitivity.IDENTIFIER
    )


def _identifier_constraints(algebra, facts: QueryFacts) -> list[Violation]:
    """R1: no expression may pin a patient graph name or an identifier to values that are not identifiers.

    The model cannot invent a graph name or identifier digits, so a comparison, a string match, a custom
    function, a VALUES block or a BIND that ties an identifier-level variable to such values came from the
    request, wherever the expression sits (FILTER, BIND, ORDER BY, GROUP BY or an aggregate argument). The
    bare patient-graph prefix, a digit-free literal in a string matcher, is house style and stays allowed.
    """
    graph_variables = _graph_variables(algebra)
    found: list[Violation] = []
    for expr in _expressions(algebra):
        for node in _comparisons_in(expr):
            pinned = _pinned_identifiers(node, facts)
            if pinned is not None:
                found.append(Violation("R1", _pin_message(node, *pinned, graph_variables)))
    for node in _nodes_named(algebra, "values"):
        for variable in sorted({variable for row in node.res for variable in row}):
            if facts.sensitivity.get(variable) is Sensitivity.IDENTIFIER:
                found.append(Violation("R1", f"VALUES pins {_label(variable, graph_variables)} to fixed values."))
    for node in _nodes_named(algebra, "Extend"):
        # Only variables that name a graph: BIND(IRI("...") AS ?g) before GRAPH ?g. A plain variable on the
        # right is rdflib's Extend(?g, __agg_N__) for a projected GROUP BY ?g, never a leak.
        if node.var in graph_variables and not isinstance(node.expr, Variable) and _carries_no_identifier(node.expr, facts):
            found.append(Violation("R1", f"BIND assigns a fixed value to {_label(node.var, graph_variables)}."))
    return found


def _expressions(algebra):
    """Every expression of the query: FILTER, BIND, ORDER BY, GROUP BY keys and aggregate arguments."""
    if isinstance(algebra, (list, tuple)):
        for item in algebra:
            yield from _expressions(item)
        return
    if not isinstance(algebra, CompValue):
        return
    if algebra.name in EXISTS:
        yield from _expressions(exists_pattern(algebra))
        return
    if algebra.name in ("Filter", "Extend"):
        yield algebra.expr
    elif algebra.name == "OrderBy":
        for condition in algebra.expr or []:
            yield condition.expr
    elif algebra.name == "Group" and algebra.expr:
        yield list(algebra.expr)
    elif algebra.name == "AggregateJoin":
        for aggregate in algebra.A:
            yield aggregate.vars
    for key, value in algebra.items():
        if not key.startswith("_") and key != "PV":
            yield from _expressions(value)


def _comparisons_in(expr):
    """Comparison nodes anywhere in an expression: relational operators, string matchers, custom functions."""
    if isinstance(expr, (list, tuple)):
        for item in expr:
            yield from _comparisons_in(item)
        return
    if not isinstance(expr, CompValue) or expr.name in EXISTS:
        return  # the patterns inside EXISTS are walked by _expressions
    if expr.name in STRING_MATCHERS or expr.name == CUSTOM_FUNCTION:
        yield expr
    elif expr.name == "RelationalExpression" and expr.other is not None:
        yield expr  # rdflib wraps plain expressions in a RelationalExpression without `other`
    for key, value in expr.items():
        if not key.startswith("_"):
            yield from _comparisons_in(value)


def _operands(node: CompValue) -> list:
    if node.name == "RelationalExpression":
        other = node.other if isinstance(node.other, list) else [node.other]
        return [node.expr, *other]
    if node.name == "Builtin_REGEX":
        return [node.text, node.pattern]
    if node.name in STRING_MATCHERS:
        return [node.arg1, node.arg2]
    return list(node.expr or [])


def _pinned_identifiers(node: CompValue, facts: QueryFacts) -> tuple[set[Variable], list] | None:
    """The identifier variables that `node` pins, and the operands it pins them to; None when it pins nothing."""
    operands = _operands(node)
    identifier_sides = [operand for operand in operands if _identifier_variables(operand, facts)]
    other_sides = [operand for operand in operands if not _identifier_variables(operand, facts)]
    if not identifier_sides or not other_sides:
        return None
    if not all(_carries_no_identifier(operand, facts) for operand in other_sides):
        return None  # ?g != ?g2, or an identifier compared with a record: nothing fixed by the request
    if node.name != "RelationalExpression" and all(_bare_prefix(operand) for operand in other_sides):
        return None  # FILTER(STRSTARTS(STR(?g), "http://example.org/graph/")): the house-style prefix
    variables = set().union(*(_identifier_variables(operand, facts) for operand in identifier_sides))
    return variables, other_sides


def _bare_prefix(operand) -> bool:
    return isinstance(operand, Literal) and not any(ch.isdigit() for ch in str(operand))


def _identifier_variables(expr, facts: QueryFacts) -> set[Variable]:
    """Every variable inside expr that can hold a patient graph name or an identifier."""
    found: set[Variable] = set()
    for variable in _variables_in(expr):
        # __agg_N__ names repeat across scopes (GUARD.md section 7): aggregate results are left to _projection.
        if not str(variable).startswith("__agg_") and facts.sensitivity.get(variable) is Sensitivity.IDENTIFIER:
            found.add(variable)
    return found


def _carries_no_identifier(expr, facts: QueryFacts) -> bool:
    """True when nothing in expr can hold a record IRI or an identifier: constants, categories, values."""
    if isinstance(expr, Variable):  # before the str check: rdflib terms are str subclasses
        if str(expr).startswith("__agg_"):
            return False  # its recorded level may belong to another scope
        return facts.sensitivity.get(expr, Sensitivity.CONSTANT) < Sensitivity.INSTANCE
    if expr is None or isinstance(expr, (Literal, URIRef, str)):
        return True
    if isinstance(expr, (list, tuple)):
        return all(_carries_no_identifier(item, facts) for item in expr)
    if isinstance(expr, CompValue):
        if expr.name in EXISTS:
            return True
        return all(_carries_no_identifier(value, facts) for key, value in expr.items() if not key.startswith("_"))
    return True


def _pin_message(node: CompValue, variables: set[Variable], other_sides: list, graph_variables: set[Variable]) -> str:
    what = " and ".join(_label(variable, graph_variables) for variable in sorted(variables))
    constants = [operand for operand in other_sides if isinstance(operand, (Literal, URIRef))]
    if len(constants) == len(other_sides):
        values = ", ".join(f'"{constant}"' for constant in constants)
        target = f"the value{'s' if len(constants) > 1 else ''} {values}"
    else:
        target = "a value that is not an identifier"
    if node.name == "RelationalExpression":
        return f"The query compares {what} with {target}."
    return f"The query matches {what} against {target}."


def _label(variable: Variable, graph_variables: set[Variable]) -> str:
    kind = "patient graph name" if variable in graph_variables else "identifier"
    return f"the {kind} ?{variable}"


def _graph_variables(algebra) -> set[Variable]:
    """Every variable used as a GRAPH term: each one names a patient graph, whatever it is called."""
    return {node.term for node in _nodes_named(algebra, "Graph") if isinstance(node.term, Variable)}


def _nodes_named(algebra, name: str):
    if isinstance(algebra, (list, tuple)):
        for item in algebra:
            yield from _nodes_named(item, name)
    elif isinstance(algebra, CompValue):
        if algebra.name == name:
            yield algebra
        if algebra.name in EXISTS:
            yield from _nodes_named(exists_pattern(algebra), name)
            return
        for key, value in algebra.items():
            if not key.startswith("_"):
                yield from _nodes_named(value, name)


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


def _projection(parsed: ParsedQuery, facts: QueryFacts, kinds: PropertyKinds) -> list[Violation]:
    first_select = FIRST_SELECT_RE.search(parsed.text)
    if first_select and SELECT_STAR_RE.match(parsed.text, first_select.start()):
        return [Violation("R2", "SELECT * returns raw rows. Only aggregated results are allowed.")]
    projected, extends, aggregate_join, group_exprs = _top_projection(parsed.algebra)
    if aggregate_join is None:
        names = ", ".join(f"?{v}" for v in projected)
        found = [Violation("R2", f"The query returns individual rows ({names}) instead of aggregated results.")]
        identifier_variables = [
            variable for variable in projected
            if facts.sensitivity.get(variable, Sensitivity.CONSTANT) >= Sensitivity.INSTANCE
        ]
        if identifier_variables:
            labels = ", ".join(f"?{variable}" for variable in identifier_variables)
            found.append(Violation("R5", f"The query exposes patient or record identifiers ({labels})."))
        return found
    group_vars = {expr for expr in group_exprs if isinstance(expr, Variable)}
    exposed = _exposed_group_keys(projected, extends, facts, group_vars)
    all_extends = {node.var: node.expr for node in _nodes_named(parsed.algebra, "Extend")}
    graph_variables = _graph_variables(parsed.algebra)
    found: list[Violation] = []
    for expr in group_exprs:
        level = expression_sensitivity(expr, facts)
        label = f"?{expr}" if isinstance(expr, Variable) else "an expression"
        if level >= Sensitivity.INSTANCE:
            rule = "R5" if expr in exposed else "R2"
            message = f"Grouping by {label} returns one row per patient or record."
        elif level is Sensitivity.TIMESTAMP:
            rule = "R2"
            message = f"Grouping by the exact timestamp {label} singles out individual records."
        else:
            continue
        if isinstance(expr, Variable):
            message += " " + _grouping_advice(expr, facts, all_extends, kinds, graph_variables)
        found.append(Violation(rule, message, grouping=True))
    for variable in projected:
        expr = extends.get(variable)
        if expr is None:
            if variable not in group_vars:
                found.append(Violation("R2", f"?{variable} is returned per row without aggregation."))
            continue
        found += _expression_violations(variable, expr, facts, group_vars)
    return found


def _grouping_advice(variable: Variable, facts: QueryFacts, extends: dict, kinds: PropertyKinds, graph_variables: set[Variable]) -> str:
    """Say where a GROUP BY key comes from and what to group by instead, so that the retry can fix it."""
    origin = _origin(variable, facts, extends)
    via = f"?{variable} is built from ?{origin}, which" if origin != variable else f"?{variable}"
    graph = origin if origin in graph_variables else min(graph_variables, default=Variable("g"))
    if origin in graph_variables:
        return (f"{via} is the patient graph name. Never group by or return the patient graph; count patients "
                f"with COUNT(DISTINCT ?{graph}) and {CATEGORY_ADVICE}.")
    for triple in facts.triples:
        if triple.object != origin or not isinstance(triple.predicate, URIRef) or triple.predicate == RDF.type:
            continue
        predicate = _short(triple.predicate)
        level = kinds.object_sensitivity(triple.predicate)
        if level is Sensitivity.IDENTIFIER:
            return (f"{via} is a patient identifier ({predicate}). Never group by or return it; count patients "
                    f"with COUNT(DISTINCT ?{graph}) and {CATEGORY_ADVICE}.")
        if predicate.endswith("Code"):
            subject = f"?{triple.subject}" if isinstance(triple.subject, Variable) else "?x"
            return (f"{via} is a code node ({predicate}): one node per record, not a category. Group by the code's "
                    f"class instead: {subject} {predicate} ?{origin} . ?{origin} a ?concept . GROUP BY ?concept "
                    f"(label it with VALUES (?concept ?label) {{ ... }} and GROUP BY ?label when the question "
                    f"names the categories).")
        if level is Sensitivity.INSTANCE:
            return (f"{via} is a record linked by {predicate}: a unique IRI per record. Group by the class of its "
                    f"code instead: ?{origin} sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept.")
        if level is Sensitivity.TIMESTAMP:
            return (f"{via} is an exact timestamp ({predicate}). Group by YEAR(?{origin}) or MONTH(?{origin}) "
                    f"instead: BIND(YEAR(?{origin}) AS ?year) ... GROUP BY ?year.")
    if facts.sensitivity.get(origin) is Sensitivity.IDENTIFIER:
        return (f"{via} is a patient identifier. Never group by or return it; count patients with "
                f"COUNT(DISTINCT ?{graph}) and {CATEGORY_ADVICE}.")
    for triple in facts.triples:
        if triple.subject == origin and triple.predicate == RDF.type and isinstance(triple.object, URIRef):
            return (f"{via} is a record IRI (a {_short(triple.object)}): one row per record. Do not group by it; "
                    f"count records with COUNT(DISTINCT ?{origin}) and group by a category or a code concept "
                    f"(?{origin} sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept).")
    return (f"{via} is a record IRI: one row per record. Count records with COUNT(DISTINCT ?{origin}) and group by "
            f"a category or a code concept (?x sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept).")


def _origin(variable: Variable, facts: QueryFacts, extends: dict) -> Variable:
    """Follow BIND chains (BIND(STR(?code) AS ?route)) back to the variable that gives the key its level."""
    level = facts.sensitivity.get(variable, Sensitivity.CONSTANT)
    seen = set()
    while variable in extends and variable not in seen:
        seen.add(variable)
        expr = extends[variable]
        if isinstance(expr, Variable) and expr in facts.aggregates:
            break  # rdflib carries a projected GROUP BY key as SAMPLE(?key): not a BIND
        sources = [
            source for source in _variables_in(expr)
            if not str(source).startswith("__agg_") and facts.sensitivity.get(source, Sensitivity.CONSTANT) >= level
        ]
        if not sources:
            break
        variable = sources[0]
    return variable


def _short(iri: URIRef) -> str:
    text = str(iri)
    return "sphn:" + text[len(SPHN):] if text.startswith(SPHN) else f"<{text}>"


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
        if expr.name in EXISTS:
            return  # nothing bound inside an EXISTS reaches the outside
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
