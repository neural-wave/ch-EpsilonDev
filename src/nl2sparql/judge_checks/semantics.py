"""Semantic checks and rewrites for queries that parse but ask Virtuoso the wrong question.

The query agent repeats a few mistakes that neither the parser nor the term check sees
(docs/FAILURE_ANALYSIS.md, sections 3.2 to 3.6): a date function on a record IRI, patients counted
under a record name, the same aggregate projected twice, a code node compared with its concept.
A hint costs the request its retry, so each check prefers a miss to a false alarm and says how to
fix the query. The rewrites repair the two mistakes that have exactly one correct form.

The judge applies REWRITES to the query text before parsing and runs CHECKS on the parsed query:
rewrite_<name>(text) -> (new text, names of the repairs applied); check_<name>(parsed, text) -> hints.
All of them were measured with zero hits on the official examples and the dev reference queries.
"""
from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.config import REPO_ROOT
from nl2sparql.context.schema import SchemaCards
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
DATE_DATATYPES = ("dateTime", "date")
DATE_PART_PROPERTIES = ("hasYear", "hasMonth", "hasDay")
CODE_PROPERTY_RE = re.compile(r"^has\w*Code$")
PATIENT_WORDS = ("patient", "subject", "people", "person", "individual")
RECORD_WORDS = (
    "record", "episode", "event", "stay", "case", "prescription", "diagnos", "procedure", "test", "visit",
    "admission", "administration", "intake", "sample", "measurement", "follow", "allerg", "consent", "variant",
    "surger", "radiother", "drug", "lab", "report", "entry", "entries",
)
ALIAS_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+|\d+")
RECORD_TYPE_RE = re.compile(r"\?(\w+)\s+(?:a|rdf:type)\s+(?:sphn:[A-Za-z]|<" + re.escape(SPHN) + r"[A-Za-z])")
INFERENCE_ON_VARIABLE_RE = re.compile(
    r"(\?\w+\s+(?:a|rdf:type)\s+\?(\w+))\s+OPTION\s*\(\s*inference\s+(?:\"[^\"]*\"|'[^']*')\s*\)", re.IGNORECASE
)
VALUES_HEADER_RE = re.compile(r"\bVALUES\s*(\((?:\s*\?\w+)+\s*\)|\?\w+)\s*\{", re.IGNORECASE)
COMPARED_LEFT_RE = re.compile(r"\?(\w+)\s*(?:(?<![<>!])=|!=|\bNOT\s+IN\b|\bIN\b)", re.IGNORECASE)
COMPARED_RIGHT_RE = re.compile(r"(?:(?<![<>!])=|!=)\s*\?(\w+)")
DATE_PLUS_DURATION_RE = re.compile(r"\?(\w+)\s*([+-])\s*([\"'])P(\d+)D\3(?:\^\^(?:<[^>]*>|[\w-]*:\w+))?")


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
    """YEAR() or bif:datediff over a record IRI, or a date function over a datediff number: Virtuoso
    answers DT001 'needs a datetime'."""
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
        elif _is_datediff(node):
            for argument in node.expr[1:]:
                if isinstance(argument, Variable) and argument in subjects:
                    hints.append(_datediff_hint(argument, classes))
    return list(dict.fromkeys(hints))


def check_graph_count_named_as_records(parsed: ParsedQuery, text: str) -> list[str]:
    """COUNT(DISTINCT ?g) under a column named after records counts patients, not records."""
    if parsed.algebra is None:
        return []
    record = _record_variable(parsed.algebra, text)
    hints = []
    for _, alias, aggregate in _column_aggregates(parsed.algebra):
        if aggregate.name != "Aggregate_Count" or not _is_variable(aggregate.vars, GRAPH):
            continue
        words = _alias_words(alias)
        if _mentions(words, PATIENT_WORDS) or not _mentions(words, RECORD_WORDS):
            continue
        counted = f"COUNT(DISTINCT ?{record})" if record is not None else "COUNT(DISTINCT ?record)"
        hints.append(
            f"{_render(aggregate)} counts patients, but the column is named ?{alias}. Count records with "
            f"{counted} (the record variable) and patients with COUNT(DISTINCT ?g)."
        )
    return hints


def check_duplicate_aggregate_columns(parsed: ParsedQuery, text: str) -> list[str]:
    """Two projected columns with the identical aggregate return the same number: a category is missing."""
    if parsed.algebra is None:
        return []
    columns = defaultdict(list)
    for project, alias, aggregate in _column_aggregates(parsed.algebra):
        columns[(id(project), _render(aggregate))].append(alias)
    return [
        f"columns {', '.join('?' + str(alias) for alias in aliases)} are all {rendered} and will return the same "
        "number. Count per category instead: ?x sphn:hasCode ?c . ?c a ?concept with "
        "VALUES (?concept ?label) { ... } and GROUP BY ?label."
        for (_, rendered), aliases in columns.items()
        if len(aliases) > 1
    ]


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
    """A code node is one IRI per record and never equal to a concept IRI: test its class instead."""
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
    hints = []
    for variable, comparisons in found.items():
        concept, alias = comparisons[0]
        listed = ", ".join(dict.fromkeys(_compact(iri) for iri, _ in comparisons))
        option = f" {INFERENCE_OPTION}" if str(concept).startswith(CODE_ROOTS) else ""
        subgroup = alias if alias is not None else Variable("matched")
        per_concept = ", one OPTIONAL per concept" if len(comparisons) > 1 else ""
        hints.append(
            f"?{variable} is a code node (object of sphn:{code_nodes[variable]}): one node per record, never "
            f"equal to the concept {listed}. Test its class instead{per_concept}: OPTIONAL {{ ?{variable} a "
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
    """A SNOMED CT IRI whose id is not numeric (snomed:Country) names no concept."""
    if parsed.algebra is None:
        return []
    return [
        f"{_compact(iri)} is not a SNOMED CT concept id; concept ids are numeric, e.g. snomed:223369002 "
        "from the code candidates."
        for iri in sorted(collect_iris(parsed.algebra))
        if str(iri).startswith(SNOMED) and not str(iri)[len(SNOMED):].isdigit()
    ]


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
        hint = (
            f"?{variable} is a sphn:{name} record, not a date: it stores the date as literal parts "
            f"(sphn:hasYear, sphn:hasMonth, sphn:hasDay); use ?{variable} sphn:hasYear ?year and compare "
            f"?year directly, without {function}()."
        )
        linking = _linking_record(variable, triples, classes)
        if linking is not None:
            record, prop = linking
            example = _date_variable(record, prop)
            hint += (
                f" Or use the dateTime of the record that links to it: ?{record} sphn:{prop} ?{example} "
                f"and {function}(?{example})."
            )
        return hint
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
    return SchemaCards.load(REPO_ROOT / "data" / "schema_cards.json")


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


def _date_variable(variable: Variable, prop: str) -> str:
    suffix = re.sub(r"^has|DateTime$|Date$|Time$", "", prop).lower()
    return f"{variable}_{suffix or 'date'}"


def _is_datediff(node) -> bool:
    return isinstance(node, CompValue) and node.name == "Function" and node.iri == DATEDIFF


def _datediff_unit(node) -> str:
    unit = node.expr[0] if node.expr else None
    return str(unit) if isinstance(unit, Literal) else "unit"


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


def _record_variable(algebra, text: str) -> Variable | None:
    """The subject of the first '?x a sphn:Class' pattern in the text: the record the query is about.
    rdflib reorders the triples of a group, so the text decides and the algebra is the fallback."""
    match = RECORD_TYPE_RE.search(text)
    if match:
        return Variable(match.group(1))
    for subject, predicate, obj in _triples(algebra):
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(obj, URIRef) and str(obj).startswith(SPHN):
            return subject
    return None


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


def _alias_words(alias: Variable) -> list[str]:
    return [word.lower() for word in ALIAS_WORD_RE.findall(str(alias))]


def _mentions(words: list[str], stems: tuple) -> bool:
    return any(word.startswith(stem) for word in words for stem in stems)


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
