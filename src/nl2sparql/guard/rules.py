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
# The sentence that closes the retry hint. Which one is decided per violation (Violation.advice) and, when
# the violations disagree, in this order: dropping GROUP BY beats looking for a category, which beats the
# generic request. "Group by a category" is never said for a patient-graph key: on the 17 October runs the
# model answered it by inventing categories and codes (q-013, q-071, official-07).
TOTALS_ADVICE = "The SELECT returns only totals: remove GROUP BY to get one overall row."
GROUPING_ADVICE = (
    "Group by a category (a code concept via ?c a ?concept, or a value-set property), "
    "not by the patient or the record; keep one row per category."
)
AGGREGATION_ADVICE = "Rewrite it so that it only returns results aggregated across all patients."
ADVICE_PRIORITY = (TOTALS_ADVICE, GROUPING_ADVICE, AGGREGATION_ADVICE)
# A SELECT reaches its aggregates (or the sub-select it re-projects) through these nodes only.
PROJECTION_MODIFIERS = ("OrderBy", "Filter", "Extend", "Distinct", "Reduced", "Slice", "ToMultiSet")


@dataclass(frozen=True)
class Violation:
    rule: str  # R1..R5, or PARSE when the query cannot be analysed
    message: str
    grouping: bool = False  # a GROUP BY key singles out patients or records; the retry hint says what to group by
    advice: str | None = None  # the sentence this violation wants at the end of the retry hint, if any


@dataclass(frozen=True)
class RuleSettings:
    patient_graph_prefix: str = "http://example.org/graph/"
    ontology_graphs: frozenset[str] = frozenset({"https://biomedit.ch/rdf/sphn-resource/global"})


@dataclass
class Projection:
    """What the outermost SELECT returns and where it comes from.

    `aggregates` maps a variable to the aggregate nodes it stands for: rdflib's `__agg_N__` results for a
    SELECT over its own GROUP BY, or the aliases a re-projected sub-select exports (`_exported`). Keying the
    sub-select case by alias keeps the `__agg_N__` names, which repeat in every sub-select, out of the outer
    scope.
    """

    projected: list  # the SELECT's variables, in order
    extends: dict  # ?x -> expression, for every BIND and SELECT expression between the SELECT and its aggregates
    aggregates: dict  # ?v -> the aggregate nodes it stands for
    group_exprs: list  # the GROUP BY keys the rows are made of
    aggregated: bool = False  # False: the rows come straight from patterns
    reprojected: bool = False  # True: the SELECT has no aggregates of its own but re-projects sub-selects that do

    @property
    def group_vars(self) -> set:
        return {expr for expr in self.group_exprs if isinstance(expr, Variable)}


def check_rules(parsed: ParsedQuery, kinds: PropertyKinds, settings: RuleSettings = RuleSettings()) -> list[Violation]:
    if parsed.kind == "Invalid":
        return [Violation("PARSE", f"The query could not be parsed: {parsed.error}")]
    if parsed.kind == "Update":
        return [Violation("R4", "The query modifies data. Only read-only SELECT queries are allowed.")]
    if parsed.kind != "SelectQuery":
        form = QUERY_FORMS.get(parsed.kind, parsed.kind)
        return [Violation("R4", f"The query is a {form} query. Only SELECT queries are allowed.")]
    facts = analyse(parsed.algebra, kinds, settings.ontology_graphs)
    projection = _top_projection(parsed.algebra)
    graph_variables = _graph_variables(parsed.algebra)
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
    violations += _whole_graph(facts, projection, graph_variables)
    if facts.touches_patient_data:
        violations += _minimum_group_size(parsed.algebra, facts)
        violations += _projection(parsed, facts, kinds, projection, graph_variables)
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


def _whole_graph(facts: QueryFacts, projection: Projection, graph_variables: set[Variable]) -> list[Violation]:
    patient_triples = [t for t in facts.triples if not t.in_ontology_graph]
    if not patient_triples:
        return []
    for triple in patient_triples:
        if isinstance(triple.predicate, Variable):
            continue
        if triple.predicate == RDF.type and isinstance(triple.object, Variable):
            continue
        return []  # at least one pattern restricts by a concrete property or class
    message = "The query matches the whole graph without restricting it to a concept."
    graph = _only_counts_patients(projection, facts, graph_variables)
    if graph is not None:
        # "How many patients are there?" written as COUNT(DISTINCT ?g) over ?s ?p ?o (intent-030): the retry
        # needs the pattern to write, and the number was wrong anyway because ?s ?p ?o also matches the ontology.
        triple = patient_triples[0]
        pattern = " ".join(_term(term) for term in (triple.subject, triple.predicate, triple.object))
        message += (f" {pattern} matches every named graph, the ontology graphs too (2,007 on dev, not 2,000). "
                    f"To count all patients use GRAPH ?{graph} {{ ?sex a sphn:AdministrativeSex }}.")
    return [Violation("R3", message)]


def _only_counts_patients(projection: Projection, facts: QueryFacts, graph_variables: set[Variable]) -> Variable | None:
    """The graph variable when the SELECT returns nothing but COUNT(DISTINCT ?g); None otherwise."""
    if not projection.aggregated or len(projection.projected) != 1:
        return None
    variable = projection.projected[0]
    expr = projection.extends.get(variable, variable)
    counted = set()
    for used in _inputs(expr, projection.extends):
        aggregates = projection.aggregates.get(used)
        if not aggregates:
            return None
        for aggregate in aggregates:
            if aggregate.name != "Aggregate_Count" or aggregate.distinct not in (True, "DISTINCT"):
                return None
            if not isinstance(aggregate.vars, Variable) or aggregate.vars not in graph_variables:
                return None
            counted.add(aggregate.vars)
    return min(counted) if len(counted) == 1 else None


def _term(term) -> str:
    if isinstance(term, Variable):
        return f"?{term}"
    if term == RDF.type:
        return "a"
    return _short(term) if isinstance(term, URIRef) else str(term)


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


def _projection(parsed: ParsedQuery, facts: QueryFacts, kinds: PropertyKinds, projection: Projection,
                graph_variables: set[Variable]) -> list[Violation]:
    first_select = FIRST_SELECT_RE.search(parsed.text)
    if first_select and SELECT_STAR_RE.match(parsed.text, first_select.start()):
        return [Violation("R2", "SELECT * returns raw rows. Only aggregated results are allowed.")]
    all_extends = {node.var: node.expr for node in _nodes_named(parsed.algebra, "Extend")}
    if not projection.aggregated:
        return _row_violations(projection.projected, facts, all_extends, graph_variables)
    group_vars = projection.group_vars
    exposed = _exposed_group_keys(projection)
    # Nothing but totals is returned (no key reaches the output) and every key singles out a patient, a record
    # or an exact timestamp: the fix is to drop GROUP BY, not to look for a category (q-062, q-013). A per-patient
    # sub-select re-projected as it is wants the opposite: keep it and aggregate outside (the ?g recipe).
    totals = not projection.reprojected and bool(projection.group_exprs) and not exposed and all(
        expression_sensitivity(expr, facts) >= Sensitivity.TIMESTAMP for expr in projection.group_exprs
    )
    grouping: list[Violation] = []
    for expr in projection.group_exprs:
        level = expression_sensitivity(expr, facts)
        label = f"?{expr}" if isinstance(expr, Variable) else "an expression"
        if level >= Sensitivity.INSTANCE:
            rule = "R5" if isinstance(expr, Variable) and expr in exposed else "R2"
            message = f"Grouping by {label} returns one row per patient or record."
        elif level is Sensitivity.TIMESTAMP:
            rule = "R2"
            message = f"Grouping by the exact timestamp {label} singles out individual records."
        else:
            continue
        advice = GROUPING_ADVICE
        if isinstance(expr, Variable):
            sentence, advice = _key_advice(expr, facts, all_extends, kinds, graph_variables, totals)
            message += " " + sentence
        grouping.append(Violation(rule, message, grouping=True, advice=TOTALS_ADVICE if totals else advice))
    if projection.reprojected and grouping:
        # A sub-select grouped per patient, record or timestamp and the SELECT above it re-projects those
        # rows: they are individual rows, and the sub-select's keys say what to change.
        return _row_violations(projection.projected, facts, all_extends, graph_variables) + grouping
    found = grouping
    for variable in projection.projected:
        expr = projection.extends.get(variable)
        if expr is None:
            if variable in projection.aggregates:
                expr = variable  # an alias a sub-select exports: the aggregates behind it are checked
            elif variable not in group_vars:
                found.append(Violation("R2", f"?{variable} is returned per row without aggregation."))
                continue
            else:
                continue
        found += _expression_violations(variable, expr, facts, projection)
    return found


def _row_violations(projected: list, facts: QueryFacts, all_extends: dict, graph_variables: set[Variable]) -> list[Violation]:
    """The SELECT returns rows of values, not aggregates: R2, plus R5 when a row carries an identifier."""
    names = ", ".join(f"?{variable}" for variable in projected)
    found = [Violation("R2", f"The query returns individual rows ({names}) instead of aggregated results.")]
    identifier_variables = [
        variable for variable in projected
        if facts.sensitivity.get(variable, Sensitivity.CONSTANT) >= Sensitivity.INSTANCE
    ]
    if identifier_variables:
        labels = ", ".join(f"?{variable}" for variable in identifier_variables)
        message = f"The query exposes patient or record identifiers ({labels})."
        for variable in identifier_variables:
            origin = _origin(variable, facts, all_extends)
            if origin in graph_variables:  # SELECT ?g gets the same recipe as GROUP BY ?g
                message += f" {_via(variable, origin)} is the patient graph name. {_graph_recipe(variable, origin)}"
                break
        found.append(Violation("R5", message))
    return found


def _key_advice(variable: Variable, facts: QueryFacts, extends: dict, kinds: PropertyKinds,
                graph_variables: set[Variable], totals: bool) -> tuple[str, str]:
    """Say where a GROUP BY key comes from and what to do instead, so that the retry can fix it.

    Returns the sentence for the message and the sentence that closes the retry hint. When the SELECT returns
    only totals, the key is only described: the closing sentence says to drop GROUP BY. A patient-graph key
    never gets "group by a category": the recipe is to remove it or to aggregate a per-patient sub-select.
    """
    origin = _origin(variable, facts, extends)
    via = _via(variable, origin)
    graph = origin if origin in graph_variables else min(graph_variables, default=Variable("g"))
    advice = GROUPING_ADVICE
    what = fix = None
    if origin in graph_variables:
        what, fix, advice = f"{via} is the patient graph name.", _graph_recipe(variable, graph), AGGREGATION_ADVICE
    for triple in facts.triples if what is None else []:
        if triple.object != origin or not isinstance(triple.predicate, URIRef) or triple.predicate == RDF.type:
            continue
        predicate = _short(triple.predicate)
        level = kinds.object_sensitivity(triple.predicate)
        if level is Sensitivity.IDENTIFIER:
            what = f"{via} is a patient identifier ({predicate})."
            fix = f"Never group by or return it; count patients with COUNT(DISTINCT ?{graph}) and {CATEGORY_ADVICE}."
        elif predicate.endswith("Code"):
            subject = f"?{triple.subject}" if isinstance(triple.subject, Variable) else "?x"
            what = f"{via} is a code node ({predicate}): one node per record, not a category."
            fix = (f"Group by the code's class instead: {subject} {predicate} ?{origin} . ?{origin} a ?concept . "
                   f"GROUP BY ?concept. Never list codes you were not given.")
        elif level is Sensitivity.INSTANCE:
            what = f"{via} is a record linked by {predicate}: a unique IRI per record."
            fix = f"Group by the class of its code instead: ?{origin} sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept."
        elif level is Sensitivity.TIMESTAMP:
            what = f"{via} is an exact timestamp ({predicate})."
            fix = f"Group by YEAR(?{origin}) or MONTH(?{origin}) instead: BIND(YEAR(?{origin}) AS ?year) ... GROUP BY ?year."
        if what is not None:
            break
    if what is None and facts.sensitivity.get(origin) is Sensitivity.IDENTIFIER:
        what = f"{via} is a patient identifier."
        fix = f"Never group by or return it; count patients with COUNT(DISTINCT ?{graph}) and {CATEGORY_ADVICE}."
    if what is None:
        typed = next((triple.object for triple in facts.triples
                      if triple.subject == origin and triple.predicate == RDF.type and isinstance(triple.object, URIRef)), None)
        if typed is not None:
            what = f"{via} is a record IRI (a {_short(typed)}): one row per record."
            fix = (f"Do not group by it; count records with COUNT(DISTINCT ?{origin}) and group by a category or a "
                   f"code concept (?{origin} sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept).")
        else:
            what = f"{via} is a record IRI: one row per record."
            fix = (f"Count records with COUNT(DISTINCT ?{origin}) and group by a category or a code concept "
                   f"(?x sphn:hasCode ?c . ?c a ?concept . GROUP BY ?concept).")
    return (what if totals else f"{what} {fix}"), advice


def _graph_recipe(variable: Variable, graph: Variable) -> str:
    """What to write instead of grouping by or returning the patient graph (fix F4 of the second failure analysis)."""
    return (f"For overall numbers remove ?{variable} from GROUP BY and SELECT. For per-patient numbers use "
            f"{{ SELECT ?{graph} (COUNT(DISTINCT ?x) AS ?n) WHERE {{ GRAPH ?{graph} {{ ... }} }} GROUP BY ?{graph} }} "
            f"and aggregate outside with COUNT(DISTINCT ?{graph}), AVG(?n), MAX(?n), SUM(IF(?n >= 10, 1, 0)).")


def _via(variable: Variable, origin: Variable) -> str:
    return f"?{variable} is built from ?{origin}, which" if origin != variable else f"?{variable}"


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


def _exposed_group_keys(projection: Projection) -> set:
    """Group keys whose values reach the output, directly or wrapped (e.g. STR(?g))."""
    group_vars = projection.group_vars
    exposed = {variable for variable in projection.projected if variable in group_vars}
    for variable in projection.projected:
        for used in _inputs(projection.extends.get(variable), projection.extends):
            if used in group_vars:
                exposed.add(used)
            for aggregate in projection.aggregates.get(used, ()):
                if aggregate.name == "Aggregate_Sample":  # SAMPLE(?g), rdflib's own for a projected key, or SAMPLE(STR(?g))
                    exposed.update(key for key in _variables_in(aggregate.vars) if key in group_vars)
    return exposed


def _expression_violations(variable, expr, facts: QueryFacts, projection: Projection) -> list[Violation]:
    found = []
    group_vars = projection.group_vars
    # What reaches the output: YEAR(SAMPLE(?date)) only exposes a year, SAMPLE(?date) the exact date.
    output = expression_sensitivity(expr, facts)
    for used in _inputs(expr, projection.extends):
        aggregates = projection.aggregates.get(used)
        if aggregates is None:
            if used not in group_vars:
                found.append(Violation("R2", f"?{variable} uses ?{used} outside an aggregate."))
            continue
        for aggregate in aggregates:
            if aggregate.name not in PICKING_AGGREGATES:
                continue
            if aggregate.name == "Aggregate_Sample" and _is_group_var(aggregate.vars, group_vars):
                continue  # rdflib carries projected GROUP BY keys as SAMPLE; the key check covers them
            name = AGGREGATE_NAMES[aggregate.name]
            if output >= Sensitivity.INSTANCE:
                found.append(Violation("R5", f"?{variable} uses {name} over identifiers or record IRIs."))
            elif aggregate.name == "Aggregate_GroupConcat" and output >= Sensitivity.VALUE:
                found.append(Violation("R2", f"?{variable} uses {name} to list individual values."))
            elif aggregate.name == "Aggregate_Sample" and output >= Sensitivity.TIMESTAMP:
                found.append(Violation("R2", f"?{variable} uses {name} to pick one record's exact value."))
    return found


def _is_group_var(term, group_vars: set) -> bool:
    """SAMPLE(STR(?g)) aggregates an expression, which cannot be looked up in a set: only a variable can be a key."""
    return isinstance(term, Variable) and term in group_vars


def _inputs(expr, extends: dict, stack: tuple = ()):
    """The variables an expression reads, following the BINDs and SELECT expressions of `extends` to what they read.

    `stack` holds the variables being followed, so BIND(?n AS ?n) (which Virtuoso rejects) stops at ?n.
    """
    for variable in _variables_in(expr):
        if variable in extends and variable not in stack:
            yield from _inputs(extends[variable], extends, (*stack, variable))
        else:
            yield variable


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


def _top_projection(algebra) -> Projection:
    return _projection_of(algebra.p)


def _projection_of(node) -> Projection:
    """The projection of the SELECT whose algebra starts at `node`: a Project, possibly under Slice or Distinct."""
    while isinstance(node, CompValue) and node.name in ("Slice", "Distinct", "Reduced"):
        node = node.p
    if not isinstance(node, CompValue) or node.name != "Project":
        return Projection([], {}, {}, [])
    projected = list(node.PV)
    extends: dict = {}
    current = node.p
    while isinstance(current, CompValue) and current.name in PROJECTION_MODIFIERS:
        if current.name == "Extend":
            extends[current.var] = current.expr
        current = current.p
    if isinstance(current, CompValue) and current.name == "AggregateJoin":
        group = current.p
        aggregates = {aggregate.res: [aggregate] for aggregate in current.A}
        return Projection(projected, extends, aggregates, list(group.expr) if group.expr else [], aggregated=True)
    inner = _reprojected(current)
    if inner is None:
        return Projection(projected, extends, {}, [])
    return Projection(projected, extends, inner.aggregates, inner.group_exprs, aggregated=True, reprojected=True)


def _reprojected(node) -> Projection | None:
    """The projection a SELECT without aggregates of its own re-projects: a sub-select whose rows are aggregates,
    or several of them joined (q-052: totals from two sub-selects, their ratio in a BIND above them).

    A UNION of such sub-selects is left as individual rows: the one dev query with that shape (gen-024, "walk me
    through all of it, record by record") answers a forbidden request, and this rule is what stops it.
    """
    if not isinstance(node, CompValue):
        return None
    if node.name in ("ToMultiSet", "Slice", "Distinct", "Reduced"):
        return _reprojected(node.p)
    if node.name in ("Join", "LeftJoin"):
        left, right = _reprojected(node.p1), _reprojected(node.p2)
        return None if left is None or right is None else _merged(left, right)
    if node.name != "Project":
        return None
    inner = _projection_of(node)
    return _exported(inner) if inner.aggregated else None


def _exported(inner: Projection) -> Projection:
    """What a sub-select hands to the SELECT above it: its GROUP BY keys and, per projected alias, the aggregates
    behind it. An alias that also reads a plain variable (which Virtuoso rejects) is not exported."""
    aggregates: dict = {}
    group_vars = inner.group_vars
    for variable in inner.projected:
        expr = inner.extends.get(variable)
        if expr is None:
            if variable in inner.aggregates:
                aggregates[variable] = inner.aggregates[variable]  # exported by a sub-select of its own
            continue  # a GROUP BY key, or unbound
        behind: list = []
        for used in _inputs(expr, inner.extends):
            if used in inner.aggregates:
                behind += inner.aggregates[used]
            elif used not in group_vars:
                break
        else:
            aggregates[variable] = behind
    return Projection(inner.projected, {}, aggregates, inner.group_exprs, aggregated=True, reprojected=True)


def _merged(left: Projection, right: Projection) -> Projection:
    """Two re-projected sub-selects side by side: the rows are the product of their groups."""
    aggregates = {variable: list(behind) for variable, behind in left.aggregates.items()}
    for variable, behind in right.aggregates.items():
        aggregates.setdefault(variable, []).extend(behind)
    projected = left.projected + [variable for variable in right.projected if variable not in left.projected]
    group_exprs = left.group_exprs + [expr for expr in right.group_exprs if expr not in left.group_exprs]
    return Projection(projected, {}, aggregates, group_exprs, aggregated=True, reprojected=True)
