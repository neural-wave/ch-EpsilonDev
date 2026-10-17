"""Semantic checks and rewrites for queries that parse but ask Virtuoso the wrong question.

The query agent repeats a few mistakes that neither the parser nor the term check sees
(docs/FAILURE_ANALYSIS.md, sections 3.2 to 3.6): a date function on a record IRI, patients counted
under a record name, the same aggregate projected twice, a code node compared with its concept, a
statistic scaled like a percentage, a share that is 100% by construction, a group key or a measure that
only an unlinked OPTIONAL block binds. A hint costs the request its retry, so each check prefers a miss
to a false alarm and says how to fix the query. The rewrites repair the two mistakes that have exactly
one correct form.

The hints are what the model sees on its retry, and the 17 October runs showed it follows them to the
letter: a generic template ("?x sphn:hasCode ?c ... VALUES") replaced the right property, and a
patient-level remedy was applied to columns that count records. Every hint here is therefore built from
the query's own variables, classes, properties and codes, never from a template with placeholders.

The judge applies REWRITES to the query text before parsing and runs CHECKS on the parsed query:
rewrite_<name>(text) -> (new text, names of the repairs applied); check_<name>(parsed, text) -> hints.
All of them were measured with zero hits on the official examples and the dev reference queries.
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from functools import lru_cache

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.config import REPO_ROOT
from nl2sparql.context.schema import SchemaCards, words
from nl2sparql.judge_checks import scope
from nl2sparql.judge_checks.scope import PATIENT_WORDS, RECORD_WORDS, _alias_words, _mentions  # noqa: F401 (shared vocabulary)
from nl2sparql.sparql.parse import ParsedQuery
from nl2sparql.sparql.prefixes import INFERENCE_RULE_SET, SPHN, STANDARD_PREFIXES, add_missing_prefixes
from nl2sparql.sparql.terms import collect_iris

SNOMED = STANDARD_PREFIXES["snomed"]
CODE_ROOTS = (STANDARD_PREFIXES["icd-10-gm"], STANDARD_PREFIXES["chop"])
TERMINOLOGY_ROOTS = CODE_ROOTS + (
    SNOMED, STANDARD_PREFIXES["loinc"], STANDARD_PREFIXES["ucum"], "http://purl.obolibrary.org/obo/",
)
INFERENCE_OPTION = f'OPTION (inference "{INFERENCE_RULE_SET}")'
GRAPH = Variable("g")
PSEUDO_IDENTIFIER = URIRef(SPHN + "hasSubjectPseudoIdentifier")
IDENTIFIER = URIRef(SPHN + "hasIdentifier")
DATEDIFF = URIRef("bif:datediff")
DATE_FUNCTIONS = {
    "Builtin_YEAR": "YEAR", "Builtin_MONTH": "MONTH", "Builtin_DAY": "DAY",
    "Builtin_HOURS": "HOURS", "Builtin_MINUTES": "MINUTES", "Builtin_SECONDS": "SECONDS",
}
# Virtuoso's own date parts return numbers: YEAR(bif:year(?start)) fails with DT001 (q-054's retry).
DATE_PART_FUNCTIONS = {
    URIRef(f"bif:{name}") for name in
    ("year", "month", "day", "dayofmonth", "dayofweek", "dayofyear", "hour", "minute", "second", "quarter", "week")
}
DATE_DATATYPES = ("dateTime", "date")
DATE_PART_PROPERTIES = ("hasYear", "hasMonth", "hasDay")
CODE_PROPERTY_RE = re.compile(r"^has\w*Code$")
RECORD_TYPE_RE = re.compile(r"\?(\w+)\s+(?:a|rdf:type)\s+(?:sphn:[A-Za-z]|<" + re.escape(SPHN) + r"[A-Za-z])")
INFERENCE_ON_VARIABLE_RE = re.compile(
    r"(\?\w+\s+(?:a|rdf:type)\s+\?(\w+))\s+OPTION\s*\(\s*inference\s+(?:\"[^\"]*\"|'[^']*')\s*\)", re.IGNORECASE
)
VALUES_HEADER_RE = re.compile(r"\bVALUES\s*(\((?:\s*\?\w+)+\s*\)|\?\w+)\s*\{", re.IGNORECASE)
COMPARED_LEFT_RE = re.compile(r"\?(\w+)\s*(?:(?<![<>!])=|!=|\bNOT\s+IN\b|\bIN\b)", re.IGNORECASE)
COMPARED_RIGHT_RE = re.compile(r"(?:(?<![<>!])=|!=)\s*\?(\w+)")
DATE_PLUS_DURATION_RE = re.compile(r"\?(\w+)\s*([+-])\s*([\"'])P(\d+)D\3(?:\^\^(?:<[^>]*>|[\w-]*:\w+))?")
# Column aliases that compare a per-patient count with a threshold ("patients_with_more_than_one").
THRESHOLD_OPERATORS = (
    ("least", ">="), ("more", ">"), ("over", ">"), ("above", ">"), ("than", ">"),
    ("fewer", "<"), ("less", "<"), ("under", "<"), ("below", "<"), ("most", "<="),
)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
# Column aliases whose constants may legitimately scale the value: percentages, rates per thousand.
RATIO_WORDS = ("pct", "percent", "percentage", "share", "rate", "ratio", "proportion", "fraction", "permille")
PERCENT_WORDS = ("pct", "percent", "percentage", "share")
SCALE_UNITS = ("thousand", "hundred", "mille", "cent")
ROUNDING = {"Builtin_ROUND", "Builtin_FLOOR", "Builtin_CEIL", "Builtin_ABS"}
OPERATOR_NODES = {
    "MultiplicativeExpression", "AdditiveExpression", "RelationalExpression", "ConditionalAndExpression",
    "ConditionalOrExpression",
}
AGGREGATE_NOUNS = {
    "Aggregate_Avg": "average", "Aggregate_Max": "maximum", "Aggregate_Min": "minimum", "Aggregate_Sum": "sum",
    "Aggregate_Count": "count",
}
WHOLE_NUMBERS = ("maximum", "minimum", "count")
YEAR_RANGE = (1800, 2100)
POSITIVE_TESTS = {(">", 0), (">=", 1), ("!=", 0)}
MIRRORED = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "=", "!=": "!="}


# ---------------------------------------------------------------------------------------- rewrites
def rewrite_inference_on_variable_class(text: str) -> tuple[str, list[str]]:
    """Strip OPTION (inference) from '?x a ?class': a code node's asserted type is its concrete code,
    and every superclass the option adds becomes an extra row or group. The option stays on concrete
    codes, and on class variables that VALUES or a comparison pins to a code, where the hierarchy is
    the point."""
    pinned = _values_variables(text) | _compared_variables(text)

    def strip(match: re.Match) -> str:
        return match.group(0) if match.group(2) in pinned else match.group(1)

    rewritten = INFERENCE_ON_VARIABLE_RE.sub(strip, text)
    return rewritten, (["inference_on_variable_class"] if rewritten != text else [])


def rewrite_date_plus_duration(text: str) -> tuple[str, list[str]]:
    """'?date + "P90D"' is NULL in Virtuoso; bif:dateadd("day", 90, ?date) is what was meant."""

    def dateadd(match: re.Match) -> str:
        variable, sign, _, days = match.groups()
        return f'bif:dateadd("day", {"-" if sign == "-" else ""}{int(days)}, ?{variable})'

    rewritten = DATE_PLUS_DURATION_RE.sub(dateadd, text)
    if rewritten == text:
        return text, []
    return add_missing_prefixes(rewritten), ["date_plus_duration"]


def _values_variables(text: str) -> set[str]:
    return {name for header in VALUES_HEADER_RE.findall(text) for name in re.findall(r"\?(\w+)", header)}


def _compared_variables(text: str) -> set[str]:
    return set(COMPARED_LEFT_RE.findall(text)) | set(COMPARED_RIGHT_RE.findall(text))


# ------------------------------------------------------------------------------------------ checks
def check_date_function_on_record(parsed: ParsedQuery, text: str) -> list[str]:
    """YEAR() or bif:datediff over a record IRI, a date function over a datediff number or over one of
    Virtuoso's own date parts (YEAR(bif:year(?start))): Virtuoso answers DT001 'needs a datetime'."""
    if parsed.algebra is None:
        return []
    triples = _triples(parsed.algebra)
    subjects = {subject for subject, _, _ in triples if isinstance(subject, Variable)}
    classes = _typed_classes(triples)
    durations = {
        node.var: _datediff_unit(node.expr)
        for node in _nodes(parsed.algebra)
        if node.name == "Extend" and _is_datediff(node.expr)
    }
    # A SELECT expression reads a grouped variable through a SAMPLE alias (__agg_N__): render the variable.
    samples = {
        aggregate.res: aggregate
        for node in _nodes(parsed.algebra) if node.name == "AggregateJoin"
        for aggregate in node.A if aggregate.name == "Aggregate_Sample"
    }
    hints = []
    for node in _nodes(parsed.algebra):
        if node.name in DATE_FUNCTIONS:
            function = DATE_FUNCTIONS[node.name]
            argument = node.arg
            if isinstance(argument, Variable) and argument in subjects:
                hints.append(_date_function_hint(argument, function, classes, triples))
            elif isinstance(argument, Variable) and argument in durations:
                hints.append(
                    f"?{argument} is already a number of {durations[argument]}s (bif:datediff), not a date: "
                    f"use ?{argument} without {function}()."
                )
            elif _is_datediff(argument):
                hints.append(
                    f"bif:datediff returns a number of {_datediff_unit(argument)}s, not a date: "
                    f"use it without {function}()."
                )
            elif _is_date_part(argument):
                inner = _render_expression(argument.expr[0], samples) if argument.expr else "?date"
                hints.append(
                    f"{function}({_render_expression(argument, samples)}): {_compact(argument.iri)} already returns a "
                    f"number, but {function}() needs a dateTime (Virtuoso DT001): write {function}({inner})."
                )
        elif _is_datediff(node):
            for argument in node.expr[1:]:
                if isinstance(argument, Variable) and argument in subjects:
                    hints.append(_datediff_hint(argument, classes))
    return list(dict.fromkeys(hints))


def check_graph_count_named_as_records(parsed: ParsedQuery, text: str) -> list[str]:
    """COUNT(DISTINCT ?g) under a column named after records counts patients, not records. The record
    variable named in the remedy is one the column's SELECT can read; when it is hidden in a sub-select
    (q-003's ?case), the hint says to export it instead of naming it as if it were in scope."""
    if parsed.algebra is None:
        return []
    hints = []
    for project, alias, aggregate in _record_named_graph_counts(parsed.algebra):
        record, exported = _record_for(project, parsed.algebra, text)
        if record is None:
            counted = "COUNT(DISTINCT ?record) (the record variable)"
        elif exported:
            counted = f"COUNT(DISTINCT ?{record}) (the record variable)"
        else:
            counted = f"COUNT(DISTINCT ?{record}) (the record variable: add ?{record} to the sub-select's SELECT)"
        hints.append(
            f"{_render(aggregate)} counts patients, but the column is named ?{alias}. Count records with "
            f"{counted} and patients with COUNT(DISTINCT ?g)."
        )
    return hints


def check_duplicate_aggregate_columns(parsed: ParsedQuery, text: str) -> list[str]:
    """Two projected columns with the identical aggregate return the same number.

    The remedy comes from the query: a per-patient count exported by a sub-select next to an alias
    with a threshold word is SUM(IF(?count > n, 1, 0)) (q-035); otherwise the query's own class, code
    property and codes become the VALUES to group by (q-047). A pair of which one column is named after
    records is left to the record-name hint (q-052), which explains the difference better.
    """
    if parsed.algebra is None:
        return []
    record_named = {(id(project), alias) for project, alias, _ in _record_named_graph_counts(parsed.algebra)}
    columns: dict[tuple[int, str], list[Variable]] = defaultdict(list)
    projects: dict[int, CompValue] = {}
    for project, alias, aggregate in _column_aggregates(parsed.algebra):
        columns[(id(project), _render(aggregate))].append(alias)
        projects[id(project)] = project
    hints = []
    for (key, rendered), aliases in columns.items():
        if len(aliases) < 2 or any((key, alias) in record_named for alias in aliases):
            continue
        listed = ", ".join(f"?{alias}" for alias in aliases)
        remedy = _duplicate_remedy(projects[key], aliases, rendered, text)
        hints.append(f"columns {listed} are all {rendered} and will return the same number. {remedy}")
    return hints


def check_unconditional_subgroup(parsed: ParsedQuery, text: str) -> list[str]:
    """COUNT(DISTINCT ?x) next to COUNT(DISTINCT ?g) where BIND(?g AS ?x) applies to every row: the two
    counts are equal, and Virtuoso's SR066 rejects the pair."""
    if parsed.algebra is None:
        return []
    hints = []
    for node in _nodes(parsed.algebra):
        if node.name != "AggregateJoin":
            continue
        counts = [agg for agg in node.A if agg.name == "Aggregate_Count" and agg.distinct]
        if not any(_is_variable(agg.vars, GRAPH) for agg in counts):
            continue
        aliases = _unconditional_graph_aliases(node.p)
        for agg in counts:
            if isinstance(agg.vars, Variable) and agg.vars in aliases:
                hints.append(
                    f"COUNT(DISTINCT ?{agg.vars}) is always equal to COUNT(DISTINCT ?g): BIND(?g AS ?{agg.vars}) "
                    "applies to every row, so the share is 100% by construction. Keep the whole cohort in the "
                    f"first GRAPH ?g {{ ... }} block and put the subgroup in OPTIONAL {{ GRAPH ?g {{ ... }} "
                    f"BIND (?g AS ?{agg.vars}) }}."
                )
    return list(dict.fromkeys(hints))


def check_pseudo_identifier_join(parsed: ParsedQuery, text: str) -> list[str]:
    """Two records joined on a hasSubjectPseudoIdentifier variable that nothing reads: records of one
    patient already share GRAPH ?g, and the join is re-evaluated per row (150x slower on q-018)."""
    return [
        f"?{pid} only joins {' and '.join(_render(subject) for subject in subjects)} on the patient. Records of "
        "the same patient are already in the same GRAPH ?g: remove the sphn:hasSubjectPseudoIdentifier join and "
        "match the second record in GRAPH ?g alone."
        for pid, subjects in _unread_pseudo_identifiers(parsed).items()
        if len(subjects) > 1
    ]


def check_unused_pseudo_identifier(parsed: ParsedQuery, text: str) -> list[str]:
    """A hasSubjectPseudoIdentifier triple whose variable nothing reads: one lookup per record for nothing.
    Harmless for the result, so the judge may treat it as advice."""
    return [
        f"?{pid} (sphn:hasSubjectPseudoIdentifier of {_render(subjects[0])}) is never used: remove that triple. "
        "Records of the same patient are already in the same GRAPH ?g."
        for pid, subjects in _unread_pseudo_identifiers(parsed).items()
        if len(subjects) == 1
    ]


def check_code_node_compared_with_concept(parsed: ParsedQuery, text: str) -> list[str]:
    """A code node is one IRI per record and never equal to a concept IRI: test its class instead.

    When the comparison labels a GROUP BY key (BIND(IF(?code = A00.0, "classical", ...)) AS ?type) the
    remedy is the VALUES that binds the key from the code's class, with the query's own labels; the
    subgroup form (one OPTIONAL per concept) is for comparisons that count a subgroup (q-050).
    """
    if parsed.algebra is None:
        return []
    code_nodes = {}
    for _, predicate, obj in _triples(parsed.algebra):
        if isinstance(obj, Variable) and isinstance(predicate, URIRef) and str(predicate).startswith(SPHN):
            name = str(predicate)[len(SPHN):]
            if CODE_PROPERTY_RE.match(name):
                code_nodes.setdefault(obj, name)
    found = defaultdict(list)
    for expression, alias in _relational_expressions(parsed.algebra):
        if expression.op not in ("=", "!=", "IN", "NOT IN"):
            continue
        sides = [expression.expr]
        sides += list(expression.other) if isinstance(expression.other, list) else [expression.other]
        variables = [side for side in sides if isinstance(side, Variable) and side in code_nodes]
        concepts = [side for side in sides if isinstance(side, URIRef) and str(side).startswith(TERMINOLOGY_ROOTS)]
        if variables and concepts:
            found[variables[0]].append((concepts[0], alias))
    group_keys = _group_keys(parsed.algebra)
    hints = []
    for variable, comparisons in found.items():
        concept, alias = comparisons[0]
        concepts = list(dict.fromkeys(iri for iri, _ in comparisons))
        listed = ", ".join(_compact(iri) for iri in concepts)
        option = f" {INFERENCE_OPTION}" if str(concept).startswith(CODE_ROOTS) else ""
        lead = (
            f"?{variable} is a code node (object of sphn:{code_nodes[variable]}): one node per record, never "
            f"equal to the concept {listed}."
        )
        if alias is not None and alias in group_keys:
            labels = _if_labels(parsed.algebra, variable)
            rows = " ".join(f'({_compact(iri)} "{labels.get(iri) or "..."}")' for iri in concepts)
            class_variable = _class_variable(parsed.algebra, variable)
            hints.append(
                f"{lead} Bind ?{alias} from its class instead: VALUES (?{class_variable} ?{alias}) {{ {rows} }} "
                f"?{variable} a ?{class_variable}{option} . and GROUP BY ?{alias}."
            )
            continue
        subgroup = alias if alias is not None else Variable("matched")
        per_concept = ", one OPTIONAL per concept" if len(comparisons) > 1 else ""
        hints.append(
            f"{lead} Test its class instead{per_concept}: OPTIONAL {{ ?{variable} a "
            f"{_compact(concept)}{option} . BIND (?g AS ?{subgroup}) }} and COUNT(DISTINCT ?{subgroup})."
        )
    return hints


def check_values_codes_without_inference(parsed: ParsedQuery, text: str) -> list[str]:
    """VALUES binds ICD-10-GM or CHOP concepts to a class variable: the type pattern on it needs the
    inference option, which the judge's concrete-code check cannot see."""
    if parsed.algebra is None:
        return []
    code_variables = set()
    for node in _nodes(parsed.algebra):
        if node.name == "values":
            for row in node.res:
                for variable, value in row.items():
                    if isinstance(value, URIRef) and str(value).startswith(CODE_ROOTS):
                        code_variables.add(variable)
    hints = []
    for subject, predicate, obj in _triples(parsed.algebra):
        if predicate != RDF.type or not isinstance(subject, Variable) or obj not in code_variables:
            continue
        option = re.compile(
            rf"\?{re.escape(str(subject))}\s+(?:a|rdf:type)\s+\?{re.escape(str(obj))}\s+OPTION\s*\(\s*inference",
            re.IGNORECASE,
        )
        if not option.search(text):
            hints.append(
                f"VALUES binds ?{obj} to ICD-10-GM/CHOP codes, whose code nodes are typed with year-versioned "
                f"subcodes: write ?{subject} a ?{obj} {INFERENCE_OPTION} or only the few year-less code nodes match."
            )
    return hints


def check_fake_snomed_iri(parsed: ParsedQuery, text: str) -> list[str]:
    """A SNOMED CT IRI whose id is not numeric (snomed:Country) names no concept. The bare namespace
    (STR(snomed:) in a namespace filter) names no concept either and is fine."""
    if parsed.algebra is None:
        return []
    return [
        f"{_compact(iri)} is not a SNOMED CT concept id; concept ids are numeric, e.g. snomed:223369002 "
        "from the code candidates."
        for iri in sorted(collect_iris(parsed.algebra))
        if str(iri).startswith(SNOMED) and str(iri)[len(SNOMED):] and not str(iri)[len(SNOMED):].isdigit()
    ]


def check_scaled_statistic(parsed: ParsedQuery, text: str) -> list[str]:
    """A column that is not a percentage, whose constants multiply the value by a power of ten:
    ROUND(1000.0 * AVG(?hours)) / 10 is the percentage formula applied to an average (q-032, q-064)."""
    if parsed.algebra is None:
        return []
    hints = []
    for project, extend, aggregates in _select_expressions(parsed.algebra):
        if _mentions_ratio(_alias_words(extend.var)):
            continue
        factor = _constant_factor(extend.expr)
        if factor < 10 or not _power_of_ten(factor):
            continue
        core, rounded = _core(extend.expr, aggregates)
        if core is None:
            continue
        noun = _noun(extend.expr, aggregates)
        fixed = core if noun in WHOLE_NUMBERS or not rounded else f"ROUND(10 * {core}) / 10"
        hints.append(
            f"?{extend.var} = {_render_expression(extend.expr, aggregates)} is {factor:g} x the {noun}; "
            f"write {fixed} AS ?{extend.var}."
        )
    return hints


def check_percentage_scale(parsed: ParsedQuery, text: str) -> list[str]:
    """A percentage column whose constants do not scale the ratio by 100 (q-013: ROUND(100.0 * a / b) / 10)."""
    if parsed.algebra is None:
        return []
    hints = []
    for project, extend, aggregates in _select_expressions(parsed.algebra):
        if not _mentions(_alias_words(extend.var), PERCENT_WORDS):
            continue
        core, _ = _core(extend.expr, aggregates)
        if core is None or " / " not in core:
            continue
        factor = _constant_factor(extend.expr)
        if abs(factor - 100) < 1e-9:
            continue
        hints.append(
            f"?{extend.var} = {_render_expression(extend.expr, aggregates)} scales the ratio by {factor:g}, not "
            f"100: a percentage is ROUND(1000.0 * {core}) / 10 AS ?{extend.var}."
        )
    return hints


def check_year_constant_on_value(parsed: ParsedQuery, text: str) -> list[str]:
    """A FILTER compares a calendar year with a variable computed from a measured value (sphn:hasValue)
    through BINDs that never call YEAR(): 10 * FLOOR(?age_value / 10) buckets ages, not dates (q-059)."""
    if parsed.algebra is None:
        return []
    triples = _triples(parsed.algebra)
    values = {obj for _, predicate, obj in triples if isinstance(obj, Variable) and _predicate_ends_with(predicate, "hasValue")}
    if not values:
        return []
    binds = {
        node.var: node.expr for node in _nodes(parsed.algebra)
        if node.name == "Extend" and not str(node.var).startswith("__agg")
    }
    dates = [obj for _, predicate, obj in triples if isinstance(obj, Variable) and _predicate_ends_with(predicate, "DateTime")]
    date = min(dates, key=lambda variable: _position(variable, text)) if dates else Variable("date")
    hints: dict[Variable, str] = {}
    for node in _nodes(parsed.algebra):
        if node.name != "Filter":
            continue
        for comparison in scope._find(node.expr, "RelationalExpression"):
            for side, other in ((comparison.expr, comparison.other), (comparison.other, comparison.expr)):
                if not isinstance(side, Variable) or side in hints or side in values or not _is_year(other):
                    continue
                source = _value_source(side, binds, values)
                expression = binds.get(side)
                if source is None or expression is None:
                    continue
                suggestion = _render_expression(expression, substitutions={source: f"YEAR(?{date})"})
                hints[side] = (
                    f"?{side} is computed from ?{source} (sphn:hasValue, a measured value) but compared with the "
                    f"year {other}: compute it from a date instead, BIND({suggestion} AS ?{side})."
                )
    return list(hints.values())


def check_required_count_tested_positive(parsed: ParsedQuery, text: str) -> list[str]:
    """IF(?n > 0, ...) over a sub-select's COUNT of a record the sub-select requires: every patient it
    keeps has at least one, so the share is 100% by construction (q-068)."""
    if parsed.algebra is None:
        return []
    hints = []
    for project in scope._find(parsed.algebra, "Project"):
        tests = _if_tests(project)
        if not tests:
            continue
        for sub_select in _direct_sub_selects(project):
            if GRAPH not in sub_select.PV:
                continue
            extends, aggregate_join = scope._select_clause(sub_select)
            if aggregate_join is None:
                continue
            required = _required_variables(scope._group_pattern(aggregate_join))
            classes = _typed_classes(_triples(sub_select.p))
            for extend in extends:
                aggregate = scope._aggregate_behind(extend)
                if aggregate is None or aggregate.name != "Aggregate_Count" or extend.var not in sub_select.PV:
                    continue
                counted = aggregate.vars
                if not isinstance(counted, Variable) or counted not in required:
                    continue
                for comparison, container in tests:
                    if _tested_variable(comparison) != extend.var:
                        continue
                    lead = (
                        f"{_render_expression(container)} always equals COUNT(DISTINCT ?g)" if container is not None
                        else f"{_render_expression(comparison)} holds for every patient"
                    )
                    record = f"a sphn:{classes[counted]}" if counted in classes else f"a ?{counted} record"
                    hints.append(
                        f"{lead}: the sub-select counts ?{counted}, which it requires, so it keeps only patients "
                        f"who have {record}. Start it from every patient (GRAPH ?g {{ ?sex a sphn:AdministrativeSex }}) "
                        f"with the ?{counted} records in OPTIONAL."
                    )
    return list(dict.fromkeys(hints))


def check_backbone_patient_count(parsed: ParsedQuery, text: str) -> list[str]:
    """COUNT(DISTINCT ?g) while every other aggregate reads only variables an OPTIONAL block binds: the
    patient count includes the patients the block does not match (q-069's implant episodes). A block
    that flags a patient or a record with BIND(?g AS ?x) or BIND(?case AS ?x) is the conditional-count
    shape, where counting every patient is the point."""
    if parsed.algebra is None:
        return []
    graph_variables = {node.term for node in _nodes(parsed.algebra) if node.name == "Graph" and isinstance(node.term, Variable)}
    hints = []
    for project in scope._find(parsed.algebra, "Project"):
        _, aggregate_join = scope._select_clause(project)
        if aggregate_join is None:
            continue
        aggregates = [aggregate for aggregate in aggregate_join.A if aggregate.name != "Aggregate_Sample"]
        if not any(aggregate.name == "Aggregate_Count" and _is_variable(aggregate.vars, GRAPH) for aggregate in aggregates):
            continue
        reads = set()
        for aggregate in aggregates:
            if not _is_variable(aggregate.vars, GRAPH):
                reads |= scope._expression_variables(aggregate.vars)
        blocks = _optional_blocks(scope._group_pattern(aggregate_join))
        optional_only = set().union(*(bound for bound, _, _ in blocks)) if blocks else set()
        flags = set().union(*(aliases for _, aliases, _ in blocks)) if blocks else set()
        if not reads or not reads <= optional_only or reads & (flags | graph_variables):
            continue
        matched = [(bound, records) for bound, _, records in blocks if bound & reads]
        classes = list(dict.fromkeys(f"sphn:{cls}" for _, records in matched for _, cls in records))
        first = next((record for _, records in matched for record, _ in records), None)
        without = " or ".join(classes) if classes else "that OPTIONAL block's records"
        alias = f"{first}_patient" if first is not None else "matched_patient"
        bound = ", ".join(f"?{variable}" for variable in sorted(reads, key=str))
        hints.append(
            f"COUNT(DISTINCT ?g) counts every patient of the group, also those without {without} ({bound} "
            f"{'is' if len(reads) == 1 else 'are'} bound only inside OPTIONAL): make that block required, or count "
            f"the patients that have one with BIND(?g AS ?{alias}) inside it and COUNT(DISTINCT ?{alias})."
        )
    return list(dict.fromkeys(hints))


def check_group_key_from_unlinked_optional(parsed: ParsedQuery, text: str) -> list[str]:
    """A GROUP BY key bound only inside an OPTIONAL block that shares nothing but ?g with the rest: every
    row is paired with every record of the block, and the rows it does not match form an empty-keyed
    group (q-055's body sites). A BIND(?g AS ?x) flag is a patient-level key and is fine."""
    if parsed.algebra is None:
        return []
    hints = []
    for aggregate_join in scope._find(parsed.algebra, "AggregateJoin"):
        group = aggregate_join.p
        if not (isinstance(group, CompValue) and group.name == "Group" and group.expr):
            continue
        keys = {key for key in group.expr if isinstance(key, Variable)}
        for left_join in _find_in_scope(group.p, "LeftJoin"):
            required, optional = _bound_variables(left_join.p1), _bound_variables(left_join.p2)
            if (required & optional) - {GRAPH}:
                continue
            if optional & _hidden_variables(left_join.p1):
                continue  # the block joins on a variable a sub-select hides: the scope checks say which
            flags = {extend.var for extend in scope._find(left_join.p2, "Extend") if _is_variable(extend.expr, GRAPH)}
            for key in sorted((keys & optional) - required - flags, key=str):
                hints.append(_unlinked_key_hint(key, left_join))
    return list(dict.fromkeys(hints))


CHECKS = [
    check_date_function_on_record,
    check_graph_count_named_as_records,
    check_duplicate_aggregate_columns,
    check_unconditional_subgroup,
    check_pseudo_identifier_join,
    check_unused_pseudo_identifier,
    check_code_node_compared_with_concept,
    check_values_codes_without_inference,
    check_fake_snomed_iri,
    check_scaled_statistic,
    check_percentage_scale,
    check_year_constant_on_value,
    check_required_count_tested_positive,
    check_backbone_patient_count,
    check_group_key_from_unlinked_optional,
]
REWRITES = [rewrite_inference_on_variable_class, rewrite_date_plus_duration]


# ------------------------------------------------------------------------------------- date hints
def _date_function_hint(variable: Variable, function: str, classes: dict, triples: list) -> str:
    name = classes.get(variable)
    properties = _date_properties(name)
    if properties:
        example = _date_variable(variable, properties[0])
        listed = f" ({', '.join('sphn:' + prop for prop in properties)})" if len(properties) > 1 else ""
        return (
            f"?{variable} is a sphn:{name} record, not a date: apply {function}() to one of its dateTime "
            f"properties{listed}, e.g. ?{variable} sphn:{properties[0]} ?{example} and {function}(?{example})."
        )
    if name and _has_date_parts(name):
        # DeathDate and BirthDate hold random year, month and day parts (5 of 848 and 21 of 2000 look like
        # years), so the only usable date is the dateTime of the record that links to them (q-005).
        hint = (
            f"?{variable} is a sphn:{name} record, not a date, and its sphn:hasYear/hasMonth/hasDay parts hold no "
            "calendar values in this data: "
        )
        linking = _linking_record(variable, triples, classes)
        if linking is not None:
            record, prop = linking
            example = _date_variable(record, prop)
            return hint + (
                f"use the dateTime of the record that links to it, ?{record} sphn:{prop} ?{example}, and "
                f"{function}(?{example})."
            )
        linker = _linking_class(name)
        if linker is not None:
            cls, link, prop = linker
            record = Variable(_snake(cls))
            example = _date_variable(record, prop)
            return hint + (
                f"use the dateTime of the record that links to it, ?{record} a sphn:{cls} ; sphn:{link} ?{variable} ; "
                f"sphn:{prop} ?{example}, and {function}(?{example})."
            )
        ages = (
            "; for ages use ?age a sphn:Age ; sphn:hasQuantity ?quantity . ?quantity sphn:hasValue ?age_value"
            if name == "BirthDate" else ""
        )
        return hint + f"no record linking to it carries a dateTime, so this data has no usable date for it{ages}."
    record = f"a sphn:{name} record" if name else "a record"
    return (
        f"?{variable} is {record}, not a date: apply {function}() to one of its dateTime properties, "
        f"e.g. ?{variable} sphn:hasStartDateTime ?start."
    )


def _datediff_hint(variable: Variable, classes: dict) -> str:
    name = classes.get(variable)
    properties = _date_properties(name)
    if properties:
        listed = f" ({', '.join('sphn:' + prop for prop in properties)})" if len(properties) > 1 else ""
        return (
            f"bif:datediff needs two dateTimes, but ?{variable} is a sphn:{name} record: use one of its dateTime "
            f"properties{listed} instead, e.g. ?{variable} sphn:{properties[0]} ?{_date_variable(variable, properties[0])}."
        )
    record = f"a sphn:{name} record" if name else "a record"
    return (
        f"bif:datediff needs two dateTimes, but ?{variable} is {record}: use one of its dateTime properties "
        f"instead, e.g. ?{variable} sphn:hasStartDateTime ?start."
    )


@lru_cache(maxsize=1)
def _cards() -> SchemaCards:
    """The schema cards, with the value sets when assets/value_sets.json is present (code labels for the hints)."""
    return SchemaCards.load(REPO_ROOT / "data" / "schema_cards.json", REPO_ROOT / "assets" / "value_sets.json")


def _date_properties(name: str | None) -> list[str]:
    if name is None:
        return []
    properties = _cards().classes.get(name, {}).get("properties", {})
    return [prop for prop, info in properties.items() if info.get("datatype") in DATE_DATATYPES]


def _has_date_parts(name: str) -> bool:
    properties = _cards().classes.get(name, {}).get("properties", {})
    return any(prop in properties for prop in DATE_PART_PROPERTIES)


def _linking_record(variable: Variable, triples: list, classes: dict) -> tuple[Variable, str] | None:
    """The record that links to the variable (?death sphn:hasDate ?death_date) with a date property."""
    for subject, _, obj in triples:
        if obj == variable and isinstance(subject, Variable):
            properties = _date_properties(classes.get(subject))
            if properties:
                return subject, properties[0]
    return None


def _linking_class(name: str) -> tuple[str, str, str] | None:
    """(class, link property, dateTime property) of a class whose records link to the named one."""
    for cls, card in _cards().classes.items():
        for prop, info in card["properties"].items():
            if info.get("target") == name:
                properties = _date_properties(cls)
                if properties:
                    return cls, prop, properties[0]
    return None


def _link_property(name: str, target: str) -> str | None:
    """The property of the class that links to the target class, when the cards know one."""
    for prop, info in _cards().classes.get(name, {}).get("properties", {}).items():
        if info.get("target") == target:
            return prop
    return None


def _code_labels(name: str, prop: str) -> dict[str, str]:
    """Full IRI -> label of the value set the cards hold for the class property, empty without one."""
    return {
        _full_iri(entry["value"]): entry.get("label") or ""
        for entry in _cards().value_sets.get(name, {}).get(prop, [])
        if entry.get("value")
    }


def _full_iri(value: str) -> str:
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1]
    prefix, separator, local = value.partition(":")
    if separator and prefix != "bif" and prefix in STANDARD_PREFIXES:
        return STANDARD_PREFIXES[prefix] + local
    return value


def _snake(name: str) -> str:
    return "_".join(words(name)) or "record"


def _date_variable(variable: Variable, prop: str) -> str:
    suffix = re.sub(r"^has|DateTime$|Date$|Time$", "", prop).lower()
    return f"{variable}_{suffix or 'date'}"


def _is_datediff(node) -> bool:
    return isinstance(node, CompValue) and node.name == "Function" and node.iri == DATEDIFF


def _is_date_part(node) -> bool:
    return isinstance(node, CompValue) and node.name == "Function" and node.iri in DATE_PART_FUNCTIONS


def _datediff_unit(node) -> str:
    unit = node.expr[0] if node.expr else None
    return str(unit) if isinstance(unit, Literal) else "unit"


# ------------------------------------------------------------------------------- record columns
def _record_named_graph_counts(algebra) -> list[tuple[CompValue, Variable, CompValue]]:
    """(SELECT, alias, aggregate) of every COUNT(DISTINCT ?g) column named after records, not patients."""
    found = []
    for project, alias, aggregate in _column_aggregates(algebra):
        if aggregate.name != "Aggregate_Count" or not _is_variable(aggregate.vars, GRAPH):
            continue
        alias_words = _alias_words(alias)
        if _mentions(alias_words, PATIENT_WORDS) or not _mentions(alias_words, RECORD_WORDS):
            continue
        found.append((project, alias, aggregate))
    return found


def _record_for(project: CompValue, algebra, text: str) -> tuple[Variable | None, bool]:
    """The record variable a SELECT should count, and whether its scope can read it: the first typed
    record in scope, else the first typed record of the query with a flag that a sub-select hides it."""
    records = _typed_records(algebra, text)
    if not records:
        return None, True
    in_scope = scope._in_scope(project.p)
    for record in records:
        if record in in_scope:
            return record, True
    record = records[0]
    hidden = any(record in scope._in_scope(sub_select.p) for sub_select in scope._find(project.p, "Project"))
    return record, not hidden


def _typed_records(algebra, text: str) -> list[Variable]:
    """The subjects of the '?x a sphn:Class' patterns in text order: the records the query is about.
    rdflib reorders the triples of a group, so the text decides and the algebra is the fallback."""
    found = [Variable(name) for name in RECORD_TYPE_RE.findall(text)]
    for subject, predicate, obj in _triples(algebra):
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(obj, URIRef) and str(obj).startswith(SPHN):
            found.append(subject)
    return list(dict.fromkeys(found))


def _record_variable(algebra, text: str) -> Variable | None:
    """The subject of the first '?x a sphn:Class' pattern in the text: the record the query is about."""
    records = _typed_records(algebra, text)
    return records[0] if records else None


# ------------------------------------------------------------------------------ duplicate columns
def _duplicate_remedy(project: CompValue, aliases: list[Variable], rendered: str, text: str) -> str:
    thresholds = [(alias, _threshold(alias)) for alias in aliases if _threshold(alias) is not None]
    counts = _per_patient_counts(project.p)
    if thresholds and counts and rendered == "COUNT(DISTINCT ?g)":
        count = counts[0]
        side = {">": "above", ">=": "above", "<": "below", "<=": "below"}
        sides = sorted({side[operator] for _, (operator, _) in thresholds})
        parts = "; ".join(f"SUM(IF(?{count} {operator} {number}, 1, 0)) AS ?{alias}" for alias, (operator, number) in thresholds)
        return (
            f"Keep COUNT(DISTINCT ?g) for the total and count the patients {' or '.join(sides)} the threshold: "
            f"{parts}."
        )
    category = _category_pattern(project, text)
    if category:
        return f"Count per category instead: {category}."
    conditions = "; ".join(
        f"OPTIONAL {{ GRAPH ?g {{ <condition for ?{alias}> }} BIND(?g AS ?{alias}_patient) }} and COUNT(DISTINCT ?{alias}_patient)"
        for alias in aliases
    )
    return f"Each column needs its own condition: {conditions}."


def _threshold(alias: Variable) -> tuple[str, int] | None:
    """('>', 1) for ?patients_with_more_than_one, ('>=', 2) for ?at_least_two; None without a threshold word."""
    alias_words = _alias_words(alias)
    operator = next((operator for word, operator in THRESHOLD_OPERATORS if word in alias_words), None)
    if operator is None:
        return None
    number = next(
        (int(word) if word.isdigit() else NUMBER_WORDS[word] for word in alias_words if word.isdigit() or word in NUMBER_WORDS),
        1,
    )
    return operator, number


def _per_patient_counts(pattern) -> list[Variable]:
    """The COUNT or SUM aliases that sub-selects of the pattern export per patient (SELECT ?g (COUNT(...) AS ?n))."""
    found = []
    for sub_select in scope._find(pattern, "Project"):
        if GRAPH not in sub_select.PV:
            continue
        extends, aggregate_join = scope._select_clause(sub_select)
        if aggregate_join is None:
            continue
        for extend in extends:
            aggregate = scope._aggregate_behind(extend)
            if aggregate is not None and aggregate.name in ("Aggregate_Count", "Aggregate_Sum") and extend.var in sub_select.PV:
                found.append(extend.var)
    return found


def _category_pattern(project: CompValue, text: str) -> str | None:
    """The query's own record, code property, code node and codes as the VALUES to group by, or None
    when the query matches no code node on a typed record."""
    triples = _triples(project.p)
    classes = _typed_classes(triples)
    concepts: dict[Variable, list[URIRef]] = defaultdict(list)
    class_variables: dict[Variable, Variable] = {}
    for subject, predicate, obj in triples:
        if predicate == RDF.type and isinstance(subject, Variable):
            if isinstance(obj, URIRef) and str(obj).startswith(TERMINOLOGY_ROOTS) and obj not in concepts[subject]:
                concepts[subject].append(obj)
            elif isinstance(obj, Variable):
                class_variables.setdefault(subject, obj)
    code_nodes = []
    for subject, predicate, obj in triples:
        if not (isinstance(obj, Variable) and isinstance(subject, Variable) and isinstance(predicate, URIRef)):
            continue
        name = str(predicate)[len(SPHN):] if str(predicate).startswith(SPHN) else ""
        if CODE_PROPERTY_RE.match(name) and subject in classes and (subject, name, obj) not in code_nodes:
            code_nodes.append((subject, name, obj))
    if not code_nodes:
        return None
    code_nodes.sort(key=lambda item: (-len(concepts[item[2]]), _position(item[2], text)))
    subject, prop, code = code_nodes[0]
    codes = concepts[code]
    labels = _code_labels(classes[subject], prop)
    if codes:
        rows = " ".join(f'({_compact(iri)} "{labels.get(str(iri)) or "..."}")' for iri in codes)
    else:
        rows = '(<concept> "label") ... one row per column, from the code candidates'
    option = f" {INFERENCE_OPTION}" if any(str(iri).startswith(CODE_ROOTS) for iri in codes) else ""
    concept = class_variables.get(code, Variable("concept"))
    return (
        f"?{subject} a sphn:{classes[subject]} ; sphn:{prop} ?{code} . ?{code} a ?{concept}{option} . "
        f"VALUES (?{concept} ?label) {{ {rows} }} and GROUP BY ?label, with one (COUNT(DISTINCT ?g) AS ?patients) column"
    )


# ---------------------------------------------------------------------------- code node labels
def _group_keys(algebra) -> set[Variable]:
    found = set()
    for node in _nodes(algebra):
        if node.name == "Group" and node.expr:
            found |= {key for key in node.expr if isinstance(key, Variable)}
    return found


def _if_labels(algebra, variable: Variable) -> dict[URIRef, str]:
    """Concept -> the label an IF(?code = concept, "label", ...) chain gives it."""
    labels = {}
    for node in _nodes(algebra):
        if node.name != "Builtin_IF" or not isinstance(node.arg2, Literal):
            continue
        for comparison in scope._find(node.arg1, "RelationalExpression"):
            if comparison.op != "=":
                continue
            sides = (comparison.expr, comparison.other)
            if variable in sides:
                for side in sides:
                    if isinstance(side, URIRef):
                        labels.setdefault(side, str(node.arg2))
    return labels


def _class_variable(algebra, code: Variable) -> Variable:
    """The variable '?code a ?x' already binds to the code's class, or ?concept."""
    for subject, predicate, obj in _triples(algebra):
        if subject == code and predicate == RDF.type and isinstance(obj, Variable):
            return obj
    return Variable("concept")


# ------------------------------------------------------------------------------- scaled columns
def _select_expressions(algebra) -> list[tuple[CompValue, CompValue, dict]]:
    """(SELECT, Extend, __agg_N__ -> aggregate) for every SELECT expression that is not a projected key."""
    found = []
    for project in scope._find(algebra, "Project"):
        extends, aggregate_join = scope._select_clause(project)
        aggregates = {aggregate.res: aggregate for aggregate in aggregate_join.A} if aggregate_join is not None else {}
        for extend in reversed(extends):  # rdflib stacks the last SELECT expression outermost
            if extend.var in project.PV and not scope._is_projected_key(extend):
                found.append((project, extend, aggregates))
    return found


def _mentions_ratio(alias_words: list[str]) -> bool:
    """pct, share, rate ... or 'per' followed by a number or a unit (per_1000, per_thousand)."""
    if _mentions(alias_words, RATIO_WORDS):
        return True
    return any(
        word == "per" and index + 1 < len(alias_words) and (alias_words[index + 1].isdigit() or alias_words[index + 1] in SCALE_UNITS)
        for index, word in enumerate(alias_words)
    )


def _constant_factor(node) -> float:
    """The constant the expression multiplies its value by: ROUND(1000.0 * X) / 10 -> 100, ROUND(100 * X) / 100 -> 1."""
    if isinstance(node, CompValue):
        if node.name == "MultiplicativeExpression":
            factor = _constant_factor(node.expr)
            for operator, other in zip(node.op or [], node.other or []):
                value = _constant_factor(other)
                factor = factor * value if operator == "*" else factor / value
            return factor
        if node.name in ROUNDING:
            return _constant_factor(node.arg)
        return 1.0
    number = _number_of(node)
    return number if number else 1.0


def _power_of_ten(factor: float) -> bool:
    return abs(factor - round(factor)) < 1e-9 and str(int(round(factor))).rstrip("0") == "1"


def _core(node, aggregates: dict) -> tuple[str | None, bool]:
    """The expression without its constant multipliers and divisors, and whether it was rounded;
    (None, ...) when nothing but constants is left."""
    rounded = False
    while isinstance(node, CompValue) and node.name in ROUNDING:
        rounded = rounded or node.name != "Builtin_ABS"
        node = node.arg
    if isinstance(node, CompValue) and node.name == "MultiplicativeExpression":
        operands = [("*", node.expr)] + list(zip(node.op or [], node.other or []))
        kept = [(operator, operand) for operator, operand in operands if _number_of(operand) is None]
        if not kept:
            return None, rounded
        parts = []
        for index, (operator, operand) in enumerate(kept):
            if isinstance(operand, CompValue) and (operand.name in ROUNDING or operand.name == "MultiplicativeExpression"):
                inner, inner_rounded = _core(operand, aggregates)
                rounded = rounded or inner_rounded
                if inner is None:
                    continue
                inner = f"({inner})" if " " in inner and len(kept) > 1 else inner
            else:
                inner = _render_expression(operand, aggregates)
                if isinstance(operand, CompValue) and operand.name in OPERATOR_NODES:
                    inner = f"({inner})"
            parts.append(inner if index == 0 else f"{operator} {inner}")
        if not parts:
            return None, rounded
        if parts[0].startswith("/ "):
            parts[0] = "1 " + parts[0]
        return " ".join(parts), rounded
    if _number_of(node) is not None:
        return None, rounded
    return _render_expression(node, aggregates), rounded


def _noun(node, aggregates: dict) -> str:
    """What the column holds: an average, a maximum, a ratio ... from the aggregate under its constants."""
    while isinstance(node, CompValue) and node.name in ROUNDING:
        node = node.arg
    if isinstance(node, CompValue) and node.name == "MultiplicativeExpression":
        operands = [("*", node.expr)] + list(zip(node.op or [], node.other or []))
        kept = [(operator, operand) for operator, operand in operands if _number_of(operand) is None]
        if any(operator == "/" for operator, _ in kept[1:]):
            return "ratio"
        return _noun(kept[0][1], aggregates) if kept else "value"
    if isinstance(node, Variable) and node in aggregates:
        return AGGREGATE_NOUNS.get(aggregates[node].name, "value")
    if isinstance(node, CompValue) and node.name.startswith("Aggregate_"):
        return AGGREGATE_NOUNS.get(node.name, "value")
    return "value"


def _number_of(term) -> float | None:
    """The value of a numeric literal, or None for anything else (an ill-typed literal stays a Literal)."""
    if not isinstance(term, Literal):
        return None
    value = term.toPython()
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return float(value)


# ------------------------------------------------------------------------------- year constants
def _predicate_ends_with(predicate, suffix: str) -> bool:
    """True for sphn:hasValue and for a property path ending in it (hasQuantity/hasValue)."""
    return str(predicate).rstrip(">)").endswith(suffix)


def _is_year(term) -> bool:
    number = _number_of(term)
    return number is not None and number == int(number) and YEAR_RANGE[0] <= number <= YEAR_RANGE[1]


def _value_source(variable: Variable, binds: dict, values: set, depth: int = 0) -> Variable | None:
    """The hasValue variable the variable derives from through BINDs that never apply a date function."""
    if variable in values:
        return variable
    expression = binds.get(variable)
    if expression is None or depth > 4:
        return None
    if any(node.name in DATE_FUNCTIONS or _is_date_part(node) for node in _nodes(expression)):
        return None
    for read in sorted(_variables(expression), key=str):
        source = _value_source(read, binds, values, depth + 1)
        if source is not None:
            return source
    return None


def _position(variable: Variable, text: str) -> int:
    position = re.search(rf"[?$]{re.escape(str(variable))}(?!\w)", text)
    return position.start() if position else len(text)


# ------------------------------------------------------------------------------ required counts
def _direct_sub_selects(project: CompValue) -> list[CompValue]:
    found = []

    def visit(node: CompValue) -> bool:
        if node is not project and node.name == "Project":
            found.append(node)
            return True
        return False

    scope._walk(project.p, visit)
    return found


def _if_tests(project: CompValue) -> list[tuple[CompValue, CompValue | None]]:
    """(comparison, the aggregate it sits in or None) for every comparison inside an IF of the SELECT's
    own scope: SUM(IF(?n > 0, 1, 0)) in the SELECT, BIND(IF(?n > 0, ...)) in WHERE."""
    found = []
    extends, aggregate_join = scope._select_clause(project)
    samples = {}
    if aggregate_join is not None:
        for aggregate in aggregate_join.A:
            if aggregate.name == "Aggregate_Sample":
                samples[aggregate.res] = aggregate.vars
            for condition in scope._find(aggregate.vars, "Builtin_IF"):
                found += [(comparison, aggregate) for comparison in scope._find(condition.arg1, "RelationalExpression")]

    def visit(node: CompValue) -> bool:
        if node is not project and node.name == "Project":
            return True
        if node.name == "Extend" and not str(node.var).startswith("__agg"):
            for condition in scope._find(node.expr, "Builtin_IF"):
                found.extend((_resolve(comparison, samples), None) for comparison in scope._find(condition.arg1, "RelationalExpression"))
        return False

    scope._walk(project.p, visit)
    return found


def _resolve(comparison: CompValue, samples: dict) -> CompValue:
    """A SELECT expression reads a grouped variable through a SAMPLE alias: put the variable back."""
    if not samples:
        return comparison
    resolved = CompValue(comparison.name)
    for key, value in comparison.items():
        if key.startswith("_"):
            continue
        resolved[key] = samples.get(value, value) if isinstance(value, Variable) else value
    return resolved


def _tested_variable(comparison: CompValue) -> Variable | None:
    """The variable a comparison tests against zero (?n > 0, ?n >= 1, ?n != 0, 0 < ?n), or None."""
    left, right = comparison.expr, comparison.other
    if isinstance(right, list):
        return None
    for variable, operator, other in ((left, comparison.op, right), (right, MIRRORED.get(comparison.op), left)):
        number = _number_of(other)
        if isinstance(variable, Variable) and number is not None and (operator, number) in POSITIVE_TESTS:
            return variable
    return None


def _required_variables(pattern) -> set[Variable]:
    """Variables bound by triple patterns outside OPTIONAL, MINUS and sub-selects."""
    found = set()

    def visit(node, optional: bool = False) -> None:
        if isinstance(node, (list, tuple)):
            for item in node:
                visit(item, optional)
            return
        if not isinstance(node, CompValue) or node.name == "Project":
            return
        if node.name == "BGP":
            if not optional:
                found.update(term for triple in node.triples for term in triple if isinstance(term, Variable))
            return
        for key, value in node.items():
            if not key.startswith("_"):
                visit(value, optional or (node.name in ("LeftJoin", "Minus") and key == "p2"))

    visit(pattern)
    return found


# ------------------------------------------------------------------------------ OPTIONAL blocks
def _optional_blocks(pattern) -> list[tuple[set[Variable], set[Variable], list[tuple[Variable, str]]]]:
    """(variables only the block binds, its BIND(?x AS ?y) flags, its typed records) per OPTIONAL block
    of the pattern in query order, sub-selects excluded."""
    blocks = []

    def visit(node: CompValue) -> bool:
        if node.name == "Project":
            return True
        if node.name == "LeftJoin":
            bound = scope._in_scope(node.p2) - scope._in_scope(node.p1)
            flags = {extend.var for extend in scope._find(node.p2, "Extend") if isinstance(extend.expr, Variable)}
            records = [(variable, cls) for variable, cls in _typed_classes(_triples(node.p2)).items() if variable in bound]
            blocks.append((bound, flags, records))
        return False

    scope._walk(pattern, visit)
    blocks.reverse()  # the walk meets the last OPTIONAL first
    return blocks


def _hidden_variables(pattern) -> set[Variable]:
    """Variables bound inside the sub-selects of a pattern that they do not project."""
    hidden = set()
    for sub_select in scope._find(pattern, "Project"):
        hidden |= {variable for variable in scope._in_scope(sub_select.p) if not str(variable).startswith("__agg")} - set(sub_select.PV)
    return hidden


def _find_in_scope(node, name: str) -> list[CompValue]:
    """Every CompValue called `name` under node, without entering sub-selects."""
    found = []

    def visit(current: CompValue) -> bool:
        if current.name == "Project":
            return True
        if current.name == name:
            found.append(current)
        return False

    scope._walk(node, visit)
    return found


def _bound_variables(node) -> set[Variable]:
    """Variables a pattern binds: triple patterns, BINDs, VALUES and what its sub-selects project."""
    found = set()

    def visit(current: CompValue) -> bool:
        if current.name == "Project":
            found.update(current.PV)
            return True
        if current.name == "BGP":
            found.update(term for triple in current.triples for term in triple if isinstance(term, Variable))
        elif current.name == "Extend" and not str(current.var).startswith("__agg"):
            found.add(current.var)
        elif current.name == "values":
            for row in current.res:
                found.update(row)
        return False

    scope._walk(node, visit)
    return found


def _unlinked_key_hint(key: Variable, left_join: CompValue) -> str:
    required = _typed_classes(_triples(left_join.p1))
    optional = _typed_classes(_triples(left_join.p2))
    record, cls = next(iter(required.items()), (None, None))
    block_record, block_class = next(iter(optional.items()), (None, None))
    with_record = f"?{record}" if record is not None else "the records of the main block"
    block = f"sphn:{block_class}" if block_class else "record of that block"
    link = _link_property(cls, block_class) if cls and block_class else None
    if link and record is not None and block_record is not None:
        linking = f"Link the block to the record, ?{record} sphn:{link} ?{block_record}, and"
    else:
        linking = "Link the block to the record with a shared variable, and"
    return (
        f"GROUP BY ?{key}: ?{key} is bound only inside an OPTIONAL block that shares no variable with {with_record} "
        f"(only the patient ?g), so every row is paired with every {block} of the patient and the rows without "
        f"one form an extra group with an empty ?{key}. {linking} bind ?{key} on every row (VALUES (?concept ?{key}) "
        f"{{ ... }} on its code, or FILTER(BOUND(?{key})))."
    )


# ------------------------------------------------------------------------------- algebra helpers
def _walk(node, visit) -> None:
    """Call visit on every CompValue below node, in query order; visit returns True to stop descending."""
    if isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, visit)
    elif isinstance(node, CompValue):
        if visit(node):
            return
        for key, value in node.items():
            if not key.startswith("_"):
                _walk(value, visit)


def _nodes(algebra) -> list[CompValue]:
    found = []

    def visit(node: CompValue) -> bool:
        found.append(node)
        return False

    _walk(algebra, visit)
    return found


def _triples(algebra) -> list[tuple]:
    return [triple for node in _nodes(algebra) if node.name == "BGP" for triple in node.triples]


def _typed_classes(triples: list) -> dict[Variable, str]:
    """Variable -> SPHN class name from its first '?x a sphn:Class' pattern."""
    classes = {}
    for subject, predicate, obj in triples:
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(obj, URIRef) and str(obj).startswith(SPHN):
            classes.setdefault(subject, str(obj)[len(SPHN):])
    return classes


def _column_aggregates(algebra) -> list[tuple[CompValue, Variable, CompValue]]:
    """(SELECT, alias, aggregate) for every projected column that is directly one aggregate. rdflib
    reuses __agg_N__ names across sub-selects, so aliases are resolved inside their own Project."""
    columns = []
    for project in _nodes(algebra):
        if project.name != "Project":
            continue
        extends, aggregates = {}, {}

        def visit(node: CompValue) -> bool:
            if node.name == "Project" and node is not project:
                return True
            if node.name == "Extend":
                extends[node.var] = node.expr
            if node.name == "AggregateJoin":
                aggregates.update((agg.res, agg) for agg in node.A)
                return True
            return False

        _walk(project.p, visit)
        for alias in project.PV:
            expression = extends.get(alias)
            if isinstance(expression, Variable) and expression in aggregates:
                columns.append((project, alias, aggregates[expression]))
    return columns


def _unconditional_graph_aliases(node, conditional: bool = False, found: set | None = None) -> set[Variable]:
    """Variables bound by BIND(?g AS ?x) on every row: not under OPTIONAL, UNION or MINUS."""
    if found is None:
        found = set()
    if isinstance(node, (list, tuple)):
        for item in node:
            _unconditional_graph_aliases(item, conditional, found)
    elif isinstance(node, CompValue):
        if node.name == "Extend" and not conditional and _is_variable(node.expr, GRAPH):
            found.add(node.var)
        for key, value in node.items():
            if key.startswith("_"):
                continue
            below = conditional or node.name == "Union" or (node.name in ("LeftJoin", "Minus") and key == "p2")
            _unconditional_graph_aliases(value, below, found)
    return found


def _relational_expressions(node, alias: Variable | None = None, found: list | None = None) -> list[tuple]:
    """(comparison, alias of the BIND it sits in or None), in query order."""
    if found is None:
        found = []
    if isinstance(node, (list, tuple)):
        for item in node:
            _relational_expressions(item, alias, found)
    elif isinstance(node, CompValue):
        if node.name == "RelationalExpression":
            found.append((node, alias))
        for key, value in node.items():
            if key.startswith("_"):
                continue
            if node.name == "Extend":
                _relational_expressions(value, node.var if key == "expr" else None, found)
            else:
                _relational_expressions(value, alias, found)
    return found


def _projected(algebra) -> set[Variable]:
    return {variable for node in _nodes(algebra) if node.name == "Project" for variable in node.PV}


def _expression_variables(algebra) -> set[Variable]:
    """Variables read by BIND, FILTER, HAVING, GROUP BY, ORDER BY, VALUES or an aggregate."""
    found = set()
    for node in _nodes(algebra):
        if node.name in ("Extend", "Filter", "Group", "OrderBy", "LeftJoin"):
            found |= _variables(node.expr)  # a FILTER inside OPTIONAL is the LeftJoin's condition
        elif node.name == "AggregateJoin":
            for agg in node.A:
                found |= _variables(agg.vars)
        elif node.name == "values":
            for row in node.res:
                found |= set(row)
    return found


def _unread_pseudo_identifiers(parsed: ParsedQuery) -> dict[Variable, list]:
    """Object variable of hasSubjectPseudoIdentifier -> the records it links, for the variables that no
    projection, expression or other triple reads."""
    if parsed.algebra is None:
        return {}
    triples = _triples(parsed.algebra)
    records = defaultdict(dict)
    for subject, predicate, obj in triples:
        if predicate == PSEUDO_IDENTIFIER and isinstance(obj, Variable):
            records[obj][subject] = None
    if not records:
        return {}
    read = _projected(parsed.algebra) | _expression_variables(parsed.algebra)
    return {
        pid: sorted(subjects, key=lambda subject: parsed.text.find(_render(subject)))  # text order, not rdflib's
        for pid, subjects in records.items()
        if pid not in read and _only_inert_uses(pid, triples, read)
    }


def _only_inert_uses(pid: Variable, triples: list, read: set) -> bool:
    """True when the identifier variable appears only in hasSubjectPseudoIdentifier triples, in a type
    triple, or as the subject of a hasIdentifier triple whose value nothing reads."""
    for subject, predicate, obj in triples:
        if predicate == PSEUDO_IDENTIFIER and obj == pid:
            continue
        if subject == pid and predicate == RDF.type:
            continue
        if subject == pid and predicate == IDENTIFIER and isinstance(obj, Variable) and obj not in read:
            if _occurrences(obj, triples) == 1:
                continue
        if pid in (subject, obj) or pid == predicate:
            return False
    return True


def _occurrences(variable: Variable, triples: list) -> int:
    return sum(1 for triple in triples for term in triple if isinstance(term, Variable) and term == variable)


def _variables(expression) -> set[Variable]:
    if isinstance(expression, Variable):
        return {expression}
    if isinstance(expression, (list, tuple)):
        return set().union(*(_variables(item) for item in expression)) if expression else set()
    if isinstance(expression, CompValue):
        return set().union(*(_variables(value) for key, value in expression.items() if not key.startswith("_")))
    return set()


def _is_variable(term, variable: Variable) -> bool:
    return isinstance(term, Variable) and term == variable


def _compact(iri: URIRef) -> str:
    text = str(iri)
    for prefix, namespace in sorted(STANDARD_PREFIXES.items(), key=lambda item: -len(item[1])):
        if text.startswith(namespace):
            return f"{prefix}:{text[len(namespace):]}"
    return f"<{text}>"


def _render(term) -> str:
    """Query-like text for messages and for comparing aggregates: COUNT(DISTINCT ?g), YEAR(?start)."""
    if isinstance(term, Variable):
        return f"?{term}"
    if isinstance(term, URIRef):
        return _compact(term)
    if isinstance(term, Literal):
        return f'"{term}"'
    if isinstance(term, CompValue):
        if term.name.startswith("Aggregate_"):
            distinct = "DISTINCT " if term.distinct else ""
            return f"{term.name[len('Aggregate_'):].upper()}({distinct}{_render(term.vars)})"
        arguments = [value for key, value in term.items() if not key.startswith("_")]
        if term.name.startswith("Builtin_"):
            return f"{term.name[len('Builtin_'):]}({', '.join(_render(value) for value in arguments)})"
        if term.name == "Function":
            return f"{_render(term.iri)}({', '.join(_render(value) for value in term.expr)})"
        return f"{term.name}({', '.join(_render(value) for value in arguments)})"
    if isinstance(term, (list, tuple)):
        return ", ".join(_render(item) for item in term)
    return str(term)


def _render_expression(node, aggregates: dict | None = None, substitutions: dict | None = None) -> str:
    """An expression as the model wrote it: ROUND(1000.0 * SUM(?hours) / COUNT(DISTINCT ?e)) / 10.
    __agg_N__ aliases are rendered as their aggregate; substitutions replace variables by text."""
    aggregates = aggregates or {}
    substitutions = substitutions or {}
    if isinstance(node, Variable):
        if node in substitutions:
            return substitutions[node]
        if node in aggregates:
            aggregate = aggregates[node]
            if aggregate.name == "Aggregate_Sample":
                return _render_expression(aggregate.vars, aggregates, substitutions)
            return _render_expression(aggregate, aggregates, substitutions)
        return f"?{node}"
    if isinstance(node, Literal):
        return str(node) if _number_of(node) is not None else f'"{node}"'
    if isinstance(node, URIRef):
        return _compact(node)
    if isinstance(node, (list, tuple)):
        return ", ".join(_render_expression(item, aggregates, substitutions) for item in node)
    if not isinstance(node, CompValue):
        return str(node)
    name = node.name

    def operand(child) -> str:
        rendered = _render_expression(child, aggregates, substitutions)
        return f"({rendered})" if isinstance(child, CompValue) and child.name in OPERATOR_NODES else rendered

    if name in ("MultiplicativeExpression", "AdditiveExpression"):
        parts = [operand(node.expr)]
        parts += [f"{operator} {operand(other)}" for operator, other in zip(node.op or [], node.other or [])]
        return " ".join(parts)
    if name == "RelationalExpression":
        other = node.other
        right = f"({_render_expression(other, aggregates, substitutions)})" if isinstance(other, list) else operand(other)
        return f"{operand(node.expr)} {node.op} {right}"
    if name in ("ConditionalAndExpression", "ConditionalOrExpression"):
        joiner = " && " if name == "ConditionalAndExpression" else " || "
        return joiner.join(operand(child) for child in [node.expr] + list(node.other or []))
    if name == "UnaryNot":
        return f"!{operand(node.expr)}"
    if name == "UnaryMinus":
        return f"-{operand(node.expr)}"
    if name.startswith("Aggregate_"):
        distinct = "DISTINCT " if node.distinct else ""
        inner = "*" if node.vars == "*" else _render_expression(node.vars, aggregates, substitutions)
        return f"{name[len('Aggregate_'):].upper()}({distinct}{inner})"
    if name == "Function":
        return f"{_compact(node.iri)}({', '.join(_render_expression(value, aggregates, substitutions) for value in node.expr)})"
    arguments = [value for key, value in node.items() if not key.startswith("_")]
    label = name[len("Builtin_"):] if name.startswith("Builtin_") else name
    return f"{label}({', '.join(_render_expression(value, aggregates, substitutions) for value in arguments)})"
