"""Judge checks on variable scope: what each clause may read, and what nothing binds.

SPARQL never complains about a variable that nothing binds: SELECT, GROUP BY and the aggregates work on
an empty column, which is how a query returns one empty-keyed row or an average of nothing instead of
an error. Virtuoso does reject a BIND that re-binds a variable (SQ149, SP031) and an aggregate that
reads another aggregate (SQ200), but only at execution time, which the judge never reaches. Every
check here applies the in-scope rules of SPARQL 1.1 section 18.2.1 to rdflib's algebra.

rdflib traps: Variable is a str subclass, so it is tested before anything string-like; a projected
GROUP BY key becomes Extend(?key, __agg_n__) over Aggregate_Sample, which binds nothing new; the
__agg_N__ names restart in every sub-select, so an Extend is only ever resolved against the nearest
AggregateJoin below it.

Every check takes the ParsedQuery of parse_sparql and returns hint sentences that name the variable.
A false alarm costs the request its retry, so each check prefers a miss over a doubtful hit. The remedy
of a hint is built from the query's own variables: the model follows the hint to the letter, and the
17 October runs showed a patient-level template (BIND(?g AS ?x)) copied into columns that count records.
"""
from __future__ import annotations

from decimal import Decimal
import re

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.context.schema import words
from nl2sparql.sparql.parse import ParsedQuery
from nl2sparql.sparql.prefixes import SPHN

# Words that name patients or records in a column alias, shared with the semantic checks.
PATIENT_WORDS = ("patient", "subject", "people", "person", "individual")
RECORD_WORDS = (
    "record", "episode", "event", "stay", "case", "prescription", "diagnos", "procedure", "test", "visit",
    "admission", "administration", "intake", "sample", "measurement", "follow", "allerg", "consent", "variant",
    "surger", "radiother", "drug", "lab", "report", "entry", "entries",
)
ALIAS_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+|\d+")
AGGREGATE_NAMES = {
    "Aggregate_Count": "COUNT", "Aggregate_Sum": "SUM", "Aggregate_Avg": "AVG", "Aggregate_Min": "MIN",
    "Aggregate_Max": "MAX", "Aggregate_Sample": "SAMPLE", "Aggregate_GroupConcat": "GROUP_CONCAT",
}
STATISTICS = {"Aggregate_Avg", "Aggregate_Min", "Aggregate_Max"}
TOTALS = {"Aggregate_Count", "Aggregate_Sum"}
EXISTS_NAMES = {"Builtin_EXISTS", "Builtin_NOTEXISTS"}
# The nodes rdflib stacks between a Project and its AggregateJoin: SELECT expressions, HAVING, ORDER BY, modifiers.
SELECT_CLAUSE_NAMES = {"Extend", "Filter", "OrderBy", "Distinct", "Reduced", "Slice"}
CLAUSE_ORDER = ("SELECT", "GROUP BY", "ORDER BY")
RESTRICTING_OPERATORS = {"<", ">", "<=", ">="}
MIRRORED_OPERATORS = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}
PER_PATIENT_WORDS = ("per_patient", "per_pt", "count", "num", "total", "many")
SELECT_STAR_RE = re.compile(r"\bSELECT\s+(?:DISTINCT\s+|REDUCED\s+)?\*", re.IGNORECASE)


def check_unbound_variables(parsed: ParsedQuery) -> list[str]:
    """A variable in SELECT, GROUP BY, ORDER BY or an aggregate input that nothing binds in its scope.

    Variables that only appear inside BIND or FILTER expressions are left alone: official-04's reference
    passes an unbound variable to IF() on purpose, to leave a column empty.
    """
    if parsed.kind != "SelectQuery":
        return []
    select_star = bool(SELECT_STAR_RE.search(parsed.text))  # SELECT * projects every variable; the guard rejects it
    hints = []
    for project in _find(parsed.algebra, "Project"):
        hints += _unbound_in_scope(project, select_star)
    return hints


def check_rebound_variables(parsed: ParsedQuery) -> list[str]:
    """A BIND, or a SELECT expression, whose variable is already in scope.

    Virtuoso accepts some re-bindings (and then ignores one of them) and rejects others; all are wrong.
    """
    if parsed.kind != "SelectQuery":
        return []
    hints = []
    seen = set()

    def visit(node) -> bool:
        if node.name != "Extend" or str(node.var).startswith("__agg") or _is_projected_key(node):
            return False
        if node.var in _in_scope(node.p) and node.var not in seen:
            seen.add(node.var)
            # Only a SELECT expression can hold an aggregate, so an __agg input tells the two forms apart.
            aggregated = any(str(variable).startswith("__agg") for variable in _expression_variables(node.expr))
            form = f"the SELECT expression (... AS ?{node.var})" if aggregated else f"BIND(... AS ?{node.var})"
            hints.append(
                f"{form} re-binds ?{node.var}, which is already bound in this scope: Virtuoso rejects it "
                f"(SP031 Alias ?{node.var} is defined twice). Bind the new value to a new variable name and use "
                "that name below, or merge both expressions into one."
            )
        return False

    _walk(parsed.algebra, visit)
    return hints


def check_aggregate_over_aggregate(parsed: ParsedQuery) -> list[str]:
    """An aggregate nested in another, or an aggregate over an alias that the same SELECT computes."""
    if parsed.kind != "SelectQuery":
        return []
    hints = []
    for project in _find(parsed.algebra, "Project"):
        extends, aggregate_join = _select_clause(project)
        if aggregate_join is None:
            continue
        aliases = {extend.var for extend in extends}
        bound = _in_scope(aggregate_join.p)
        for aggregate in aggregate_join.A:
            if aggregate.name == "Aggregate_Sample":
                continue
            nested = _find(aggregate.vars, None)
            if nested:
                hints.append(
                    f"{_render(aggregate, nested[0])} nests one aggregate inside another, which SPARQL does not "
                    "allow (Virtuoso: SQ200 Aggregate function not allowed in context). Compute the inner "
                    f"aggregate in a sub-select, e.g. {{ SELECT ({_render(nested[0])} AS ?total) WHERE {{ ... }} }}, "
                    "and use ?total in the outer SELECT."
                )
                continue
            for variable in sorted(_expression_variables(aggregate.vars)):
                if variable in aliases and variable not in bound:
                    hints.append(
                        f"{_render(aggregate)}: ?{variable} is an alias computed in this SELECT, and an aggregate "
                        "cannot read another aggregate's result (Virtuoso: SQ200 Aggregate function not allowed "
                        f"in context). Aggregate over a variable bound in WHERE instead, or compute ?{variable} "
                        "in a sub-select first."
                    )
    return list(dict.fromkeys(hints))


def check_filter_restricts_statistic(parsed: ParsedQuery) -> list[str]:
    """A FILTER on a per-patient aggregate that AVG, MIN or MAX also reads, next to a COUNT or SUM.

    "How many have more than N records, and the average": the filter was meant for the count, but it
    also restricts the statistic. The filtered variable must come out of a sub-select aggregate: a
    threshold on a raw value (age > 65) defines a cohort, and so does a threshold at zero (`> 0`,
    `>= 1`) on a count, which only drops the patients without the record. Without a COUNT or SUM in
    the SELECT the filter is legitimate too.
    """
    if parsed.kind != "SelectQuery":
        return []
    hints = []
    for aggregate_join in _find(parsed.algebra, "AggregateJoin"):
        statistics: dict[Variable, list[str]] = {}
        has_total = False
        for aggregate in aggregate_join.A:
            if aggregate.name in STATISTICS:
                for variable in _expression_variables(aggregate.vars):
                    statistics.setdefault(variable, []).append(_render(aggregate))
            elif aggregate.name in TOTALS:
                has_total = True
        if not statistics or not has_total:
            continue
        pattern = _group_pattern(aggregate_join)
        per_patient = _sub_select_aggregates(pattern)
        for variable, operator, value in _filter_comparisons(pattern):
            if variable not in statistics or variable not in per_patient or (operator, value) in ((">", 0), (">=", 1)):
                continue
            comparison = f"?{variable} {operator} {value}"
            hints.append(
                f"FILTER({comparison}) also restricts {', '.join(statistics[variable])}: they are computed over "
                f"the filtered rows only. Drop the FILTER and count the rows that satisfy it with "
                f"SUM(IF({comparison}, 1, 0))."
            )
    return list(dict.fromkeys(hints))


def check_optional_cross_product(parsed: ParsedQuery) -> list[str]:
    """Two OPTIONAL blocks of one group each binding a variable that AVG, SUM or COUNT reads.

    The rows of one block are repeated for every row of the other, so each average is weighted by the
    other block's record count. MIN, MAX and COUNT(DISTINCT) do not mind repeated rows.
    """
    if parsed.kind != "SelectQuery":
        return []
    hints = []
    for aggregate_join in _find(parsed.algebra, "AggregateJoin"):
        reads: dict[Variable, str] = {}
        for aggregate in aggregate_join.A:
            if aggregate.name in ("Aggregate_Avg", "Aggregate_Sum") or (
                aggregate.name == "Aggregate_Count" and not aggregate.distinct
            ):
                for variable in _expression_variables(aggregate.vars):
                    reads.setdefault(variable, _render(aggregate))
        if not reads:
            continue
        blocks: list[list[Variable]] = []

        def visit(node) -> bool:
            if node.name == "Project":
                return True  # a sub-select is its own group
            if node.name == "LeftJoin":
                touched = sorted(variable for variable in _in_scope(node.p2) - _in_scope(node.p1) if variable in reads)
                if touched:
                    blocks.append(touched)
            return False

        _walk(_group_pattern(aggregate_join), visit)
        if len(blocks) < 2:
            continue
        blocks.reverse()  # the walk meets the last OPTIONAL first
        aggregates = " and ".join(", ".join(reads[variable] for variable in block) for block in blocks)
        branches = " UNION ".join(
            f"{{ GRAPH ?g {{ ... ?{block[0]} }} BIND(?g AS ?{block[0]}_patient) }}" for block in blocks
        )
        hints.append(
            f"{aggregates} read variables bound in {len(blocks)} different OPTIONAL blocks: the rows of one block "
            "are repeated for every row of the other, so averages, sums and counts are weighted wrongly. Put each "
            f"block in its own UNION branch instead: {branches}, and count the patients of each branch with "
            f"COUNT(DISTINCT ?{blocks[0][0]}_patient)."
        )
    return hints


CHECKS = [
    check_unbound_variables,
    check_rebound_variables,
    check_aggregate_over_aggregate,
    check_filter_restricts_statistic,
    check_optional_cross_product,
]


def _unbound_in_scope(project, select_star: bool) -> list[str]:
    clauses: dict[Variable, list[str]] = {}
    aggregates: dict[tuple[str, Variable], CompValue] = {}
    extends, _ = _select_clause(project)
    aliases = {extend.var for extend in extends}  # an aggregate over an alias is check_aggregate_over_aggregate's
    # SELECT expressions only: a projected GROUP BY key is Extend(?key, __agg_n__) over SAMPLE and binds nothing.
    expression_aliases = {extend.var for extend in extends if not _is_projected_key(extend)}

    def unbound(variable, clause, bound) -> None:
        if variable not in bound and clause not in clauses.setdefault(variable, []):
            clauses[variable].append(clause)

    if not select_star:
        bound = _in_scope(project.p)
        for variable in project.PV:
            unbound(variable, "SELECT", bound)

    def visit(node) -> bool:
        if node is not project and node.name == "Project":
            return True  # a sub-select is its own scope
        if node.name == "Group":
            # Virtuoso accepts GROUP BY on a SELECT alias (SELECT (YEAR(?dt) AS ?year) ... GROUP BY ?year gave an
            # exact result on q-004); the spec does not, but Virtuoso is the target.
            bound = _in_scope(node.p) | expression_aliases
            for variable in _expression_variables(node.expr):
                unbound(variable, "GROUP BY", bound)
        elif node.name == "OrderBy":
            bound = _in_scope(node.p)
            for variable in _expression_variables(node.expr):
                unbound(variable, "ORDER BY", bound)
        elif node.name == "AggregateJoin":
            bound = _in_scope(node.p) | aliases
            for aggregate in node.A:
                if aggregate.name == "Aggregate_Sample":
                    continue
                for variable in _expression_variables(aggregate.vars):
                    if variable not in bound:
                        aggregates.setdefault((AGGREGATE_NAMES.get(aggregate.name, aggregate.name), variable), aggregate)
        return False

    _walk(project.p, visit)
    if not clauses and not aggregates:
        return []
    nested = set()
    for sub_select in _find(project.p, "Project"):
        nested |= _expression_variables(sub_select)
    hints = []
    for variable, found in clauses.items():
        where = "/".join(clause for clause in CLAUSE_ORDER if clause in found)
        if variable in nested:
            hints.append(
                f"?{variable} is used in {where} but is not bound in this scope (a sub-select only exports the "
                f"variables it projects): add ?{variable} to the sub-select's SELECT."
            )
        else:
            hints.append(
                f"?{variable} is used in {where} but nothing binds it: add BIND(<expression> AS ?{variable}) "
                "or a pattern that binds it."
            )
    for (name, variable), aggregate in aggregates.items():
        rendered = f"{name}({'DISTINCT ' if aggregate.distinct else ''}?{variable})"
        if variable in nested:
            hints.append(
                f"{rendered}: ?{variable} is not bound in this scope (a sub-select only exports the variables "
                f"it projects): add ?{variable} to the sub-select's SELECT."
            )
        elif name == "COUNT":
            hints.append(_unbound_count_hint(project, variable, rendered))
        else:
            hint = (
                f"{rendered}: nothing binds ?{variable}: add BIND(<expression> AS ?{variable}) "
                "or a pattern that binds it."
            )
            if name in ("AVG", "MAX", "MIN") and any(word in str(variable).lower() for word in PER_PATIENT_WORDS):
                hint += " " + _per_patient_sketch(project, variable)
            hints.append(hint)
    return hints


def _unbound_count_hint(project, variable, rendered: str) -> str:
    """The remedy for a COUNT over a variable nothing binds, from the alias and the records in scope.

    ?guided_proc next to a typed ?proc, or any alias named after records, counts records: the remedy
    binds the record of the pattern. Only an alias named after patients, or after nothing, gets the
    patient subgroup BIND(?g AS ?x) (q-060's ?g_with_abx); the 17 October runs copied that form into
    record columns (q-011, q-074, q-076) and the counts came out as patients.
    """
    records = _typed_records(project.p)
    name = str(variable)
    # ?guided_proc names ?proc even when a sub-select hides it (q-074): the alias was derived from the record.
    suffix = next((record for record in _typed_records(project.p, in_scope_only=False) if name != str(record) and name.endswith(f"_{record}")), None)
    if suffix is not None:
        return (
            f"{rendered}: nothing binds ?{variable}. It counts ?{suffix} records, so bind the record, not the patient: "
            f"OPTIONAL {{ GRAPH ?g {{ <condition on ?{suffix}> }} BIND(?{suffix} AS ?{variable}) }} and "
            f"COUNT(DISTINCT ?{variable})."
        )
    alias_words = _alias_words(variable)
    if _mentions(alias_words, RECORD_WORDS) and not _mentions(alias_words, PATIENT_WORDS):
        record = _matching_record(alias_words, records)
        if record is not None:
            return (
                f"{rendered}: nothing binds ?{variable}. The column counts records: bind the sphn:{records[record]} "
                f"record of that pattern, BIND(?{record} AS ?{variable}), and COUNT(DISTINCT ?{variable}); or one row "
                "per kind with VALUES (?concept ?kind) { ... } and GROUP BY ?kind."
            )
        return (
            f"{rendered}: nothing binds ?{variable}. The column counts records: COUNT(DISTINCT ?record) of that "
            "pattern, or one row per kind with VALUES (?concept ?kind) { ... } and GROUP BY ?kind."
        )
    return (
        f"{rendered}: nothing binds ?{variable}. Count a subgroup with "
        f"OPTIONAL {{ GRAPH ?g {{ <condition> }} BIND(?g AS ?{variable}) }} and COUNT(DISTINCT ?{variable})."
    )


def _typed_records(pattern, in_scope_only: bool = True) -> dict[Variable, str]:
    """Variable -> SPHN class of every '?x a sphn:Class' pattern under the pattern, by default only the
    records the scope can read (its own patterns and what its sub-selects project)."""
    records: dict[Variable, str] = {}
    in_scope = _in_scope(pattern) if in_scope_only else None
    for subject, predicate, obj in (triple for bgp in _find(pattern, "BGP") for triple in bgp.triples):
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(obj, URIRef) and str(obj).startswith(SPHN):
            if in_scope is None or subject in in_scope:
                records.setdefault(subject, str(obj)[len(SPHN):])
    return records


def _matching_record(alias_words: list[str], records: dict[Variable, str]) -> Variable | None:
    """The record whose class shares a word with the alias (?telephone_followup -> ?record a sphn:FollowUp),
    else the first record of the scope."""
    for record, cls in records.items():
        class_words = words(cls)
        if any(len(word) >= 4 and (word.startswith(part) or part.startswith(word)) for word in alias_words for part in class_words):
            return record
    return next(iter(records), None)


def _alias_words(alias: Variable) -> list[str]:
    return [word.lower() for word in ALIAS_WORD_RE.findall(str(alias))]


def _mentions(alias_words: list[str], stems: tuple) -> bool:
    return any(word.startswith(stem) for word in alias_words for stem in stems)


def _per_patient_sketch(project, variable) -> str:
    """The sub-select a per-patient statistic needs, with the record class filled in when the query names it."""
    record, cls = "?record", "sphn:<Class>"
    candidates = {}
    for subject, predicate, obj in _triples(project.p):
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(obj, URIRef) and str(obj).startswith(SPHN):
            stem = str(subject).lower()
            if len(stem) >= 4 and stem in str(variable).lower():
                candidates[subject] = obj
    if len(candidates) == 1:
        (subject, obj), = candidates.items()
        record, cls = f"?{subject}", f"sphn:{str(obj)[len(SPHN):]}"
    return (
        f"Per-patient statistics need a subquery: {{ SELECT ?g (COUNT(DISTINCT {record}) AS ?{variable}) "
        f"WHERE {{ GRAPH ?g {{ {record} a {cls} }} }} GROUP BY ?g }} then AVG(?{variable}), MAX(?{variable})."
    )


def _in_scope(node) -> set[Variable]:
    """The in-scope variables of a pattern (SPARQL 1.1 section 18.2.1): what a clause after it may read."""
    if isinstance(node, (list, tuple)):
        return set().union(*(_in_scope(item) for item in node))
    if not isinstance(node, CompValue):
        return set()
    name = node.name
    if name == "BGP":
        return {term for triple in node.triples for term in triple if isinstance(term, Variable)}
    if name == "values":
        return {variable for row in node.res for variable in row}
    if name == "Project":
        return set(node.PV)  # a sub-select exports only what it projects
    if name == "Extend":
        return _in_scope(node.p) | (set() if _is_projected_key(node) else {node.var})
    if name == "Minus":
        return _in_scope(node.p1)  # MINUS removes rows and never binds
    if name == "AggregateJoin":
        return _in_scope(node.p) | {aggregate.res for aggregate in node.A}
    if name == "ServiceGraphPattern":  # rdflib keeps the parse tree; the guard rejects SERVICE anyway
        return _expression_variables(node.graph) | ({node.term} if isinstance(node.term, Variable) else set())
    found = {node.term} if isinstance(node.get("term"), Variable) else set()  # GRAPH ?g { ... }
    for key in ("p", "p1", "p2"):  # Join, LeftJoin, Union, Graph, Group, Filter, OrderBy, Slice, Distinct, ToMultiSet
        if key in node:
            found |= _in_scope(node[key])
    return found


def _is_projected_key(extend) -> bool:
    """rdflib projects a GROUP BY key as Extend(?key, __agg_n__) over SAMPLE(?key), which binds nothing new."""
    aggregate = _aggregate_behind(extend)
    return aggregate is not None and aggregate.name == "Aggregate_Sample" and aggregate.vars == extend.var


def _aggregate_behind(extend):
    """The aggregate an Extend of an __agg_N__ variable stands for, taken from the nearest AggregateJoin below."""
    if not (isinstance(extend.expr, Variable) and str(extend.expr).startswith("__agg")):
        return None
    node = extend.p
    while isinstance(node, CompValue) and node.name in SELECT_CLAUSE_NAMES:
        node = node.p
    if isinstance(node, CompValue) and node.name == "AggregateJoin":
        for aggregate in node.A:
            if aggregate.res == extend.expr:
                return aggregate
    return None


def _select_clause(project) -> tuple[list[CompValue], CompValue | None]:
    """The Extend nodes of the SELECT expressions and the AggregateJoin they stand on, when the scope groups."""
    extends = []
    node = project.p
    while isinstance(node, CompValue) and node.name in SELECT_CLAUSE_NAMES:
        if node.name == "Extend":
            extends.append(node)
        node = node.p
    return extends, node if isinstance(node, CompValue) and node.name == "AggregateJoin" else None


def _group_pattern(aggregate_join):
    """The WHERE pattern an AggregateJoin groups: below its Group node."""
    group = aggregate_join.p
    return group.p if isinstance(group, CompValue) and group.name == "Group" else group


def _sub_select_aggregates(pattern) -> set[Variable]:
    """The variables that sub-selects of the pattern export as aggregates: per-patient counts and the like."""
    found = set()
    for sub_select in _find(pattern, "Project"):
        extends, aggregate_join = _select_clause(sub_select)
        if aggregate_join is None:
            continue
        for extend in extends:
            aggregate = _aggregate_behind(extend)
            if aggregate is not None and aggregate.name != "Aggregate_Sample" and extend.var in sub_select.PV:
                found.add(extend.var)
    return found


def _filter_comparisons(pattern) -> list[tuple[Variable, str, object]]:
    """(variable, operator, number) for every FILTER of the pattern that compares a variable with a number.

    FILTERs inside sub-selects are their own scope and a FILTER inside OPTIONAL only restricts the
    optional part; rdflib keeps the latter as the LeftJoin's expr, not as a Filter node.
    """
    found = []

    def visit(node) -> bool:
        if node.name == "Project":
            return True
        if node.name == "Filter":
            for comparison in _find(node.expr, "RelationalExpression"):
                if comparison.op not in RESTRICTING_OPERATORS:
                    continue
                left, right = comparison.expr, comparison.other
                if isinstance(left, Variable) and _is_number(right):
                    found.append((left, comparison.op, right.toPython()))
                elif isinstance(right, Variable) and _is_number(left):
                    found.append((right, MIRRORED_OPERATORS[comparison.op], left.toPython()))
        return False

    _walk(pattern, visit)
    return found


def _is_number(term) -> bool:
    return isinstance(term, Literal) and isinstance(term.toPython(), (int, float, Decimal)) and not isinstance(term.toPython(), bool)


def _render(aggregate, argument=None) -> str:
    """COUNT(DISTINCT ?g), AVG(?days), SUM(COUNT(DISTINCT ?g)): the aggregate as the model wrote it."""
    name = AGGREGATE_NAMES.get(aggregate.name, aggregate.name)
    distinct = "DISTINCT " if aggregate.distinct else ""
    if argument is not None:
        inner = _render(argument)
    elif isinstance(aggregate.vars, Variable):
        inner = f"?{aggregate.vars}"
    else:
        inner = "*" if aggregate.vars == "*" else "..."
    return f"{name}({distinct}{inner})"


def _expression_variables(expression) -> set[Variable]:
    """Variables an expression reads, not counting the pattern inside EXISTS or NOT EXISTS."""
    found = set()

    def visit(node) -> None:
        if isinstance(node, Variable):
            found.add(node)
        elif isinstance(node, (list, tuple)):
            for item in node:
                visit(item)
        elif isinstance(node, CompValue):
            if node.name not in EXISTS_NAMES:
                for child in _children(node):
                    visit(child)
        elif isinstance(node, dict):  # VALUES rows
            for key, value in node.items():
                visit(key)
                visit(value)

    visit(expression)
    return found


def _triples(pattern) -> list[tuple]:
    """The triples of every BGP of the pattern, sub-selects excluded."""
    triples = []

    def visit(node) -> bool:
        if node.name == "Project":
            return True
        if node.name == "BGP":
            triples.extend(node.triples)
        return False

    _walk(pattern, visit)
    return triples


def _find(node, name: str | None) -> list[CompValue]:
    """Every CompValue called `name` under node, or every aggregate when name is None, depth first."""
    found = []

    def visit(current) -> bool:
        if current.name == name or (name is None and current.name.startswith("Aggregate_")):
            found.append(current)
        return False

    _walk(node, visit)
    return found


def _walk(node, visit) -> None:
    """Call visit on every CompValue under node; visit returns True to stop descending."""
    if isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, visit)
    elif isinstance(node, CompValue) and not visit(node):
        for child in _children(node):
            _walk(child, visit)


def _children(node: CompValue):
    """The sub-nodes of a CompValue, skipping rdflib's cached _vars."""
    for key, value in node.items():
        if not key.startswith("_"):
            yield value
