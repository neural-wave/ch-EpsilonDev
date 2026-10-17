"""Deterministic rewrites of the aggregation mistakes the query agent makes when it counts or groups.

A judge hint buys a full regeneration of the query, which on the 17 October runs came back worse as
often as better. The second failure analysis found six aggregation mistakes with exactly one correct
form, where a rewrite flips the case reliably and no hint is needed:

- rewrite_group_by_patient_rows: a top-level GROUP BY whose keys make one row per patient or per
  record (?g, an alias of ?g, typed record variables) while every projected column is an aggregate,
  so the rows have no label: the GROUP BY goes and one overall row comes back (q-003, q-062).
- rewrite_graph_count_named_as_records: COUNT(DISTINCT ?g) under a column named after records counts
  patients. When exactly one typed record variable in the SELECT's scope shares a word stem with the
  alias (the class name's words plus the card picker's synonyms) that record is counted instead
  (q-042, q-054); otherwise the hint of judge_checks.semantics stays.
- rewrite_group_key_bound_only_in_optional: a GROUP BY key that only constant-label BINDs inside
  OPTIONAL blocks bind leaves the rows that match none of them in an extra group with an empty key:
  FILTER (BOUND(?key)) drops that group (q-050).
- rewrite_optional_bind_without_pattern: OPTIONAL { BIND(expr AS ?x) } with no pattern cannot see the
  variables bound outside it, so ?x stays empty on every row: the BIND moves out (q-064).
- rewrite_scaled_statistic: ROUND(F * AVG(?x)) / D with F/D >= 100 is the percentage formula applied
  to a plain statistic, 100 times the average: ROUND(10 * AVG(?x)) / 10, and MAX(?x) or MIN(?x) for
  the extremes. A 0/1 indicator (IF(..., 1, 0)) is a share and stays (q-064).
- rewrite_dead_code_filter: a code node already typed with a concrete code gets a second type pattern
  ?code a ?concept whose concept is neither returned nor grouped, only narrowed by a FILTER(REGEX(...))
  copied from an example that drops the codes outside its pattern: the type pattern, the BIND that
  derives the id and the filters go (q-054).

Each rewrite takes the prefix-fixed, parseable text with its ParsedQuery and returns (text, repairs):
the text unchanged and [] when it does not apply. Every rewrite re-parses its result and gives up when
the result does not parse or projects other columns than before. The judge runs REWRITES in order
after the schema rewrites and re-parses after each applied one.

All six were measured at zero hits on the 16 official examples, the 3 team examples and the 45 dev
reference queries (tests/test_rewrites_aggregation.py repeats that), and on the 17 October dev run no
rewrite touched a right answer.

rdflib traps: Variable is a str subclass; a projected GROUP BY key is Extend(?key, __agg_n__) over
Aggregate_Sample; the __agg_N__ names repeat across sub-selects, so aliases are resolved per Project.
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
import re

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.context.schema import SYNONYMS, stem, words
from nl2sparql.judge_checks.scope import (
    EXISTS_NAMES,
    SELECT_CLAUSE_NAMES,
    _aggregate_behind,
    _expression_variables,
    _find,
    _in_scope,
    _is_projected_key,
    _select_clause,
)
from nl2sparql.judge_checks.semantics import (
    CODE_PROPERTY_RE,
    PATIENT_WORDS,
    RECORD_WORDS,
    TERMINOLOGY_ROOTS,
    _alias_words,
    _column_aggregates,
    _compact,
    _is_variable,
    _mentions,
    _nodes,
    _triples,
)
from nl2sparql.sparql.parse import ParsedQuery, parse_sparql
from nl2sparql.sparql.prefixes import IRIS_AND_STRINGS_RE, SPHN

GRAPH = Variable("g")
MODIFIERS = {"Slice", "Distinct", "Reduced"}  # what rdflib stacks above the top-level Project
STATISTICS = {"AVG": "average", "MAX": "maximum", "MIN": "minimum"}
SHARE_WORDS = ("pct", "percent", "share", "rate", "ratio", "proportion", "fraction")
GUARDING_BUILTINS = ("Builtin_BOUND", "Builtin_COALESCE", "Builtin_IF")  # expressions that handle an empty key on purpose
REGEX_METACHARACTERS = ".[](){}*+?|$"
STRING_ESCAPE_RE = re.compile(r"\\([\\\"'])")  # the SPARQL string escapes rdflib leaves in a literal
COMMENT_RE = re.compile(r"#[^\n]*")
GROUP_BY_RE = re.compile(r"\bGROUP\s+BY((?:\s+\?\w+)+)", re.IGNORECASE)
OPTIONAL_RE = re.compile(r"\bOPTIONAL\s*\{", re.IGNORECASE)
BIND_RE = re.compile(r"\s*BIND\s*\(", re.IGNORECASE)
FILTER_RE = re.compile(r"\bFILTER\s*\(", re.IGNORECASE)
ALIAS_RE = re.compile(r"\s*AS\s+\?(\w+)", re.IGNORECASE)
SCALED_RE = re.compile(
    r"\bROUND\s*\(\s*(\d+(?:\.\d+)?)\s*\*\s*(AVG|MAX|MIN)\s*\(\s*\?(\w+)\s*\)\s*\)\s*/\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)


# ------------------------------------------------------------ 1. GROUP BY per patient under an aggregate-only SELECT
def rewrite_group_by_patient_rows(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """Drop a top-level GROUP BY that makes one row per patient or per record when no key is projected.

    Every projected column is an aggregate, so the groups come out as unlabeled rows, one per patient
    (?g or an alias of it among the keys, whatever else is there) or one per record (every key a typed
    record variable): "how many ..." wanted one overall row. A query that projects ?g keeps its GROUP BY,
    so a forbidden per-patient listing stays as written for the guard.
    """
    project = _top_project(parsed)
    if project is None:
        return text, []
    extends, aggregate_join = _select_clause(project)
    clause = _clause_chain(project)
    if aggregate_join is None or any(node.name == "Filter" for node in clause):  # HAVING: not a plain total
        return text, []
    group = aggregate_join.p
    if not (isinstance(group, CompValue) and group.name == "Group" and isinstance(group.expr, list) and group.expr):
        return text, []
    keys = list(group.expr)
    aggregate_columns = {extend.var for extend in extends if not _is_projected_key(extend)}
    if not all(isinstance(key, Variable) for key in keys) or any(column not in aggregate_columns for column in project.PV):
        return text, []
    ordered = set().union(*(_expression_variables(node.expr) for node in clause if node.name == "OrderBy"))
    if not ordered <= aggregate_columns:  # ORDER BY on a key would read a variable the overall row lacks
        return text, []
    patient_keys = {GRAPH} | {node.var for node in _nodes(group.p) if node.name == "Extend" and _is_variable(node.expr, GRAPH)}
    records = _typed_records(group.p)
    if not (any(key in patient_keys for key in keys) or all(key in records for key in keys)):
        return text, []
    masked = _masked(text)
    depths = _depths(masked)
    clauses = [match for match in GROUP_BY_RE.finditer(masked) if depths[match.start()] == 0]
    if len(clauses) != 1 or [Variable(name) for name in re.findall(r"\?(\w+)", clauses[0].group(1))] != keys:
        return text, []
    match = clauses[0]
    before, after = text[:match.start()].rstrip(" \t"), text[match.end():]
    if before.endswith("\n") and after.startswith("\n"):
        after = after[1:]
    rewritten = before + after
    if _accepted(parsed, rewritten) is None:
        return text, []
    listed = " ".join(f"?{key}" for key in keys)
    return rewritten, [
        f"Dropped GROUP BY {listed}: every SELECT column is an aggregate, so the groups were unlabeled rows per "
        "patient; the query now returns one overall row"
    ]


# ------------------------------------------------------------- 2. COUNT(DISTINCT ?g) under a record-named column
def rewrite_graph_count_named_as_records(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """COUNT(DISTINCT ?g) AS ?<records> counts the record variable the alias names.

    The condition is the one of judge_checks.semantics.check_graph_count_named_as_records (a record
    word and no patient word in the alias), plus exactly one typed record variable in the SELECT's
    scope whose class name, or a card picker synonym of it, shares a word stem with the alias.
    """
    if parsed.kind != "SelectQuery":
        return text, []
    rewritten, repairs = text, []
    for project, alias, aggregate in _column_aggregates(parsed.algebra):
        if aggregate.name != "Aggregate_Count" or not _is_variable(aggregate.vars, GRAPH):
            continue
        alias_words = _alias_words(alias)
        if _mentions(alias_words, PATIENT_WORDS) or not _mentions(alias_words, RECORD_WORDS):
            continue
        candidates = _records_named(project, alias_words)
        if len(candidates) != 1:
            continue
        (record, name), = candidates.items()
        column = re.compile(r"COUNT\s*\(\s*(?:DISTINCT\s+)?\?g\s*\)\s+AS\s+\?" + re.escape(str(alias)) + r"\b", re.IGNORECASE)
        if len(column.findall(rewritten)) != 1:
            continue
        rewritten = column.sub(f"COUNT(DISTINCT ?{record}) AS ?{alias}", rewritten)
        repairs.append(
            f"COUNT(DISTINCT ?g) AS ?{alias} counted patients under a record name: it now counts the "
            f"sphn:{name} records, COUNT(DISTINCT ?{record})"
        )
    if not repairs or _accepted(parsed, rewritten) is None:
        return text, []
    return rewritten, repairs


# ------------------------------------------------------------ 3. GROUP BY key bound only by labels inside OPTIONAL
def rewrite_group_key_bound_only_in_optional(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """Add FILTER (BOUND(?key)) when the top-level GROUP BY key is bound only by BIND("label" AS ?key)
    inside OPTIONAL blocks: the rows that match none of the blocks form an extra group with an empty
    key. A key that BOUND, COALESCE or IF reads is handled on purpose and stays."""
    project = _top_project(parsed)
    if project is None:
        return text, []
    _, aggregate_join = _select_clause(project)
    group = aggregate_join.p if aggregate_join is not None else None
    if not (isinstance(group, CompValue) and group.name == "Group" and isinstance(group.expr, list)):
        return text, []
    pattern = group.p
    guarded = set().union(*(_expression_variables(node) for node in _nodes(pattern) if node.name in GUARDING_BUILTINS))
    keys = [key for key in group.expr if isinstance(key, Variable) and key not in guarded and _only_optional_labels(pattern, key)]
    if not keys:
        return text, []
    masked = _masked(text)
    opening = masked.find("{")  # the top-level WHERE block: the first brace of the query
    closing = _matching_brace(masked, opening) if opening >= 0 else -1
    if closing < 0:
        return text, []
    filters = "".join(f"  FILTER (BOUND(?{key}))\n" for key in keys)
    line_start = text.rfind("\n", 0, closing) + 1
    if text[line_start:closing].strip():  # the brace shares its line with a pattern
        rewritten = text[:closing].rstrip(" \t") + "\n" + filters + text[closing:]
    else:
        indent = text[line_start:closing]
        rewritten = text[:line_start] + filters.replace("  FILTER", f"{indent}  FILTER") + text[line_start:]
    if _accepted(parsed, rewritten) is None:
        return text, []
    return rewritten, [
        f"Added FILTER (BOUND(?{key})): the GROUP BY key is bound only inside OPTIONAL blocks, so the rows "
        "that matched none of them made an extra group with an empty key"
        for key in keys
    ]


# ------------------------------------------------------------------------------ 4. OPTIONAL { BIND(...) } without a pattern
def rewrite_optional_bind_without_pattern(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """Move a BIND out of an OPTIONAL block that holds nothing else: with no pattern the block is
    evaluated on its own and cannot see the variables bound outside it (rdflib: a LeftJoin whose p2 is
    an Extend over an empty BGP)."""
    if parsed.kind != "SelectQuery":
        return text, []
    empty = [node for node in _find(parsed.algebra, "LeftJoin") if _binds_only(node)]
    if not empty:
        return text, []
    masked = _masked(text)
    rewritten, lifted = text, []
    for match in reversed(list(OPTIONAL_RE.finditer(masked))):
        opening = match.end() - 1
        closing = _matching_brace(masked, opening)
        if closing < 0 or not _bind_statements_only(masked[opening + 1:closing]):
            continue
        inner = text[opening + 1:closing].strip()
        rewritten = rewritten[:match.start()] + inner + rewritten[closing + 1:]
        lifted.append(inner)
    if len(lifted) != len(empty) or _accepted(parsed, rewritten) is None:
        return text, []
    return rewritten, [
        f"OPTIONAL {{ {statement} }} has no pattern, so the BIND could not see the variables bound outside the "
        "block: the BIND now stands on its own"
        for statement in reversed(lifted)
    ]


# ------------------------------------------------------------------------ 5. percentage scaling on a plain statistic
def rewrite_scaled_statistic(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """ROUND(F * AVG(?x)) / D with F/D >= 100 is 100 times the average (the percentage formula copied
    onto a statistic): ROUND(10 * AVG(?x)) / 10, and MAX(?x) or MIN(?x) for the extremes. The
    expression stays when something else divides it, when ?x is a 0/1 indicator (IF(..., 1, 0)) or
    when the alias names a share."""
    if parsed.kind != "SelectQuery":
        return text, []
    masked = _masked(text)
    indicators = _indicator_variables(parsed.algebra)
    rewritten, repairs = text, []
    for match in reversed(list(SCALED_RE.finditer(masked))):
        factor, function, name, divisor = match.groups()
        if float(divisor) == 0 or float(factor) / float(divisor) < 100 or Variable(name) in indicators or _in_ratio(masked, match):
            continue
        alias = ALIAS_RE.match(masked, match.end())
        if alias and _mentions(_alias_words(Variable(alias.group(1))), SHARE_WORDS):
            continue
        function = function.upper()
        replacement = f"ROUND(10 * AVG(?{name})) / 10" if function == "AVG" else f"{function}(?{name})"
        rewritten = rewritten[:match.start()] + replacement + rewritten[match.end():]
        times = float(factor) / float(divisor)
        repairs.append(
            f"{' '.join(text[match.start():match.end()].split())} is {times:g} times the {STATISTICS[function]}: "
            f"written as {replacement}"
        )
    if not repairs or _accepted(parsed, rewritten) is None:
        return text, []
    return rewritten, list(reversed(repairs))


# --------------------------------------------------------------------------- 6. dead REGEX filter on a code's concept
def rewrite_dead_code_filter(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """Delete ?code a ?concept, the BIND that derives the code id from ?concept and the FILTERs on them
    when ?code is already typed with one concrete code, nothing returns, groups or otherwise reads
    ?concept, and a FILTER(REGEX(...)) whose literal prefix is that code's own id narrows it (the subcode
    regex copied from an example, which drops the category code itself)."""
    if parsed.kind != "SelectQuery":
        return text, []
    chains = _dead_filter_chains(parsed.algebra)
    if not chains:
        return text, []
    rewritten = text
    for code, concept, family, _ in chains:
        rewritten = _delete_chain(rewritten, code, concept, family)
        if rewritten is None:
            return text, []
    reparsed = _accepted(parsed, rewritten)
    if reparsed is None:
        return text, []
    remaining = _all_variables(reparsed.algebra)
    typed = {(subject, str(obj)) for subject, predicate, obj in _triples(reparsed.algebra) if predicate == RDF.type}
    if any(family & remaining or (code, iri) not in typed for code, _, family, iri in chains):
        return text, []
    return rewritten, [
        f"Removed ?{code} a ?{concept} with the BIND and FILTER(REGEX) on it: ?{code} is already typed "
        f"{_compact(iri)}, ?{concept} is never returned, and the regex copied from an example dropped the codes "
        "outside its pattern"
        for code, concept, _, iri in chains
    ]


REWRITES = [
    rewrite_group_by_patient_rows,
    rewrite_graph_count_named_as_records,
    rewrite_group_key_bound_only_in_optional,
    rewrite_optional_bind_without_pattern,
    rewrite_scaled_statistic,
    rewrite_dead_code_filter,
]


# ------------------------------------------------------------------------------------------ algebra helpers
def _top_project(parsed: ParsedQuery):
    """The Project of the top-level SELECT, below rdflib's modifiers (LIMIT, DISTINCT)."""
    if parsed.kind != "SelectQuery" or parsed.algebra is None:
        return None
    node = parsed.algebra.p
    while isinstance(node, CompValue) and node.name in MODIFIERS:
        node = node.p
    return node if isinstance(node, CompValue) and node.name == "Project" else None


def _clause_chain(project) -> list[CompValue]:
    """The nodes rdflib stacks between a Project and its AggregateJoin or pattern: SELECT expressions,
    HAVING (a Filter), ORDER BY and the modifiers."""
    chain, node = [], project.p
    while isinstance(node, CompValue) and node.name in SELECT_CLAUSE_NAMES:
        chain.append(node)
        node = node.p
    return chain


def _projections(parsed: ParsedQuery) -> list[list[Variable]]:
    return [list(project.PV) for project in _find(parsed.algebra, "Project")]


def _accepted(parsed: ParsedQuery, rewritten: str) -> ParsedQuery | None:
    """The re-parsed rewrite, or None when it does not parse or projects other columns than before."""
    reparsed = parse_sparql(rewritten)
    if reparsed.kind != "SelectQuery" or reparsed.query is None or _projections(reparsed) != _projections(parsed):
        return None
    return reparsed


def _typed_records(pattern) -> set[Variable]:
    """Variables typed with an SPHN class anywhere in the pattern, sub-selects included."""
    return {
        subject for subject, predicate, obj in _triples(pattern)
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(obj, URIRef) and str(obj).startswith(SPHN)
    }


def _records_named(project, alias_words: list[str]) -> dict[Variable, str]:
    """Typed record variables in the SELECT's scope whose class name shares a word stem with the alias."""
    stems = {stem(word) for word in alias_words}
    in_scope = _in_scope(project.p)
    found: dict[Variable, str] = {}
    for subject, predicate, obj in _triples(project.p):
        if predicate != RDF.type or not isinstance(subject, Variable) or subject == GRAPH or subject not in in_scope:
            continue
        if isinstance(obj, URIRef) and str(obj).startswith(SPHN) and stems & _class_stems(str(obj)[len(SPHN):]):
            found.setdefault(subject, str(obj)[len(SPHN):])
    return found


@lru_cache(maxsize=None)
def _class_stems(name: str) -> frozenset[str]:
    """Stems of the class name's words and of the card picker's synonyms for it (stays -> AdministrativeCase)."""
    stems = {stem(word) for word in words(name)}
    for phrases, targets in SYNONYMS.items():
        if name in targets:
            stems |= {stem(word) for phrase in phrases for word in phrase.split()}
    return frozenset(stems)


def _only_optional_labels(pattern, key: Variable) -> bool:
    """True when every binding of the key in the pattern is BIND(<literal> AS ?key) inside an OPTIONAL block."""
    sites: list[bool] = []

    def visit(node, optional: bool) -> None:
        if isinstance(node, (list, tuple)):
            for item in node:
                visit(item, optional)
            return
        if not isinstance(node, CompValue):
            return
        name = node.name
        if name == "BGP":
            sites.extend(False for triple in node.triples if any(_is_variable(term, key) for term in triple))
            return
        if name == "values":
            sites.extend(False for row in node.res if key in row)
            return
        if name == "Project":  # a sub-select binds only what it projects
            if key in node.PV:
                sites.append(False)
            return
        if name == "Graph" and _is_variable(node.get("term"), key):
            sites.append(False)
        if name == "Extend" and node.var == key:
            sites.append(optional and isinstance(node.expr, Literal))
        for child_name, child in node.items():
            if child_name.startswith("_") or (name == "Minus" and child_name == "p2"):  # MINUS binds nothing
                continue
            visit(child, optional or (name == "LeftJoin" and child_name == "p2"))

    visit(pattern, False)
    return bool(sites) and all(sites)


def _binds_only(left_join) -> bool:
    """An OPTIONAL that holds only BINDs: no filter, and an Extend chain over an empty BGP."""
    if isinstance(left_join.expr, CompValue) and left_join.expr.name != "TrueFilter":
        return False
    node = left_join.p2
    if not (isinstance(node, CompValue) and node.name == "Extend"):
        return False
    while isinstance(node, CompValue) and node.name == "Extend":
        node = node.p
    return isinstance(node, CompValue) and node.name == "BGP" and not node.triples


def _indicator_variables(algebra) -> set[Variable]:
    """Variables bound to IF(..., 1, 0), directly or through an aggregate over it: their average is a share."""
    found = set()
    for node in _nodes(algebra):
        if node.name != "Extend":
            continue
        aggregate = _aggregate_behind(node)
        expression = aggregate.vars if aggregate is not None else node.expr
        if any(_is_indicator(branch) for branch in _find(expression, "Builtin_IF")):
            found.add(node.var)
    return found


def _is_indicator(builtin_if) -> bool:
    return all(_is_zero_or_one(builtin_if.get(arm)) for arm in ("arg2", "arg3"))


def _is_zero_or_one(term) -> bool:
    if not isinstance(term, Literal):
        return False
    value = term.toPython()
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and float(value) in (0.0, 1.0)


def _dead_filter_chains(algebra) -> list[tuple[Variable, Variable, set[Variable], str]]:
    """(code node, concept variable, concept and what BINDs derive from it, concrete code IRI) for every
    ?code a ?concept that only redundant filters read."""
    triples = _triples(algebra)
    code_nodes = {
        obj for _, predicate, obj in triples
        if isinstance(obj, Variable) and isinstance(predicate, URIRef) and str(predicate).startswith(SPHN)
        and CODE_PROPERTY_RE.match(str(predicate)[len(SPHN):])
    }
    extends = [node for node in _nodes(algebra) if node.name == "Extend" and not str(node.var).startswith("__agg")]
    found = []
    for code in sorted(code_nodes):
        types = [obj for subject, predicate, obj in triples if subject == code and predicate == RDF.type]
        concrete = [obj for obj in types if isinstance(obj, URIRef) and str(obj).startswith(TERMINOLOGY_ROOTS)]
        concepts = [obj for obj in types if isinstance(obj, Variable)]
        if len(concrete) != 1 or len(concepts) != 1:
            continue
        concept, iri = concepts[0], str(concrete[0])
        family = {concept}
        grown = True
        while grown:
            grown = False
            for extend in extends:
                if extend.var not in family and _expression_variables(extend.expr) & family:
                    family.add(extend.var)
                    grown = True
        if _only_redundant_filters_read(algebra, code, concept, family, iri):
            found.append((code, concept, family, iri))
    return found


def _only_redundant_filters_read(algebra, code: Variable, concept: Variable, family: set[Variable], iri: str) -> bool:
    """Nothing returns, groups, orders, aggregates, joins or otherwise reads the family; the FILTERs on it
    are a STRSTARTS on the code's namespace or a REGEX on the code's own id, at least one REGEX among them."""
    regex = False
    for node in _nodes(algebra):
        name = node.name
        if name == "BGP":
            for triple in node.triples:
                if triple != (code, RDF.type, concept) and any(term in family for term in triple if isinstance(term, Variable)):
                    return False
        elif name == "Extend":
            if node.var in family and not _expression_variables(node.expr) <= family:
                return False
        elif name == "Filter":  # rdflib joins the FILTERs of one group with &&: each conjunct on its own
            for conjunct in _conjuncts(node.expr):
                read = _expression_variables(conjunct)
                if read & family:
                    if not read <= family or not _redundant_filter(conjunct, iri):
                        return False
                    regex = regex or conjunct.name == "Builtin_REGEX"
        elif name == "Project":
            if family & set(node.PV):
                return False
        elif name in ("Group", "OrderBy", "LeftJoin"):
            if family & _expression_variables(node.expr):
                return False
        elif name == "AggregateJoin":
            if any(family & _expression_variables(aggregate.vars) for aggregate in node.A):
                return False
        elif name == "values":
            if any(family & set(row) for row in node.res):
                return False
        elif name == "Graph":
            if node.term in family:
                return False
        elif name in EXISTS_NAMES:
            if family & _all_variables(node):
                return False
    return regex


def _redundant_filter(expression, iri: str) -> bool:
    """STRSTARTS on a prefix of the concrete code's IRI, or REGEX whose literal prefix is the code's id
    (on the id or on the IRI): the concrete type already guarantees the match."""
    if not isinstance(expression, CompValue):
        return False
    if expression.name == "Builtin_STRSTARTS":
        prefix = _string_of(expression.arg2)
        return prefix is not None and iri.startswith(prefix)
    if expression.name == "Builtin_REGEX":
        pattern = _string_of(expression.pattern)
        literal = _literal_prefix(pattern) if pattern is not None else ""
        tail, code_id = literal.rsplit("/", 1)[-1], iri.rsplit("/", 1)[-1]
        return bool(literal) and tail == code_id and (literal == tail or iri.endswith(literal) or iri.startswith(literal))
    return False


def _conjuncts(expression) -> list:
    if isinstance(expression, CompValue) and expression.name == "ConditionalAndExpression":
        return [expression.expr] + list(expression.other or [])
    return [expression]


def _string_of(term) -> str | None:
    """The string a literal or IRI argument stands for; rdflib keeps the escapes of a string literal
    ('\\\\.' stays two characters), so they are decoded here."""
    if isinstance(term, CompValue) and term.name == "Builtin_STR":
        term = term.arg
    if isinstance(term, Literal):
        return STRING_ESCAPE_RE.sub(lambda match: match.group(1), str(term))
    return str(term) if isinstance(term, URIRef) else None


def _literal_prefix(pattern: str) -> str:
    """The characters a regex matches literally from its start: '^00\\.0[0-9]$' gives '00.0'."""
    index, out = (1 if pattern.startswith("^") else 0), []
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            if pattern[index + 1].isalnum():  # \d, \w, \s: a class, not a literal
                break
            out.append(pattern[index + 1])
            index += 2
            continue
        if char in REGEX_METACHARACTERS:
            break
        out.append(char)
        index += 1
    return "".join(out)


def _all_variables(node) -> set[Variable]:
    """Every variable below the node, VALUES rows and the parse tree inside EXISTS included."""
    found = set()

    def visit(item) -> None:
        if isinstance(item, Variable):
            found.add(item)
        elif isinstance(item, CompValue):
            for key, value in item.items():
                if not key.startswith("_"):
                    visit(value)
        elif isinstance(item, dict):
            for key, value in item.items():
                visit(key)
                visit(value)
        elif isinstance(item, (list, tuple, set)):
            for element in item:
                visit(element)

    visit(node)
    return found


# --------------------------------------------------------------------------------------------- text helpers
def _masked(text: str) -> str:
    """The text with strings, IRIs and comments blanked to spaces, so braces and keywords inside them are
    not scanned; positions are those of the original."""

    def blank(match: re.Match) -> str:
        return " " * len(match.group(0))

    return COMMENT_RE.sub(blank, IRIS_AND_STRINGS_RE.sub(blank, text))


def _depths(masked: str) -> list[int]:
    """The brace depth before each character."""
    depths, depth = [], 0
    for char in masked:
        depths.append(depth)
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depths


def _matching_brace(masked: str, opening: int) -> int:
    return _matching(masked, opening, "{", "}")


def _matching_paren(masked: str, opening: int) -> int:
    return _matching(masked, opening, "(", ")")


def _matching(masked: str, opening: int, open_char: str, close_char: str) -> int:
    depth = 0
    for index in range(opening, len(masked)):
        if masked[index] == open_char:
            depth += 1
        elif masked[index] == close_char:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _bind_statements_only(masked: str) -> bool:
    """True when the (masked) block content is one or more BIND statements and nothing else."""
    index, found = 0, 0
    while True:
        match = BIND_RE.match(masked, index)
        if match is None:
            return found > 0 and not masked[index:].strip()
        closing = _matching_paren(masked, match.end() - 1)
        if closing < 0:
            return False
        found += 1
        index = _after_dot(masked, closing + 1)


def _after_dot(masked: str, index: int) -> int:
    """The position after an optional '.' that follows a BIND or FILTER."""
    match = re.compile(r"\s*\.").match(masked, index)
    return match.end() if match else index


def _in_ratio(masked: str, match: re.Match) -> bool:
    """Something else multiplies or divides the matched expression."""
    before = masked[:match.start()].rstrip()
    after = masked[match.end():].lstrip()
    return before.endswith(("*", "/")) or after.startswith(("*", "/"))


def _delete_chain(text: str, code: Variable, concept: Variable, family: set[Variable]) -> str | None:
    """The text without ?code a ?concept, the BINDs that bind a family variable and the FILTERs that read
    only family variables; None when the type pattern is not written as one plain statement."""
    masked = _masked(text)
    edits: list[tuple[int, int, str]] = []
    type_pattern = rf"\?{re.escape(str(code))}\s+(?:a|rdf:type)\s+\?{re.escape(str(concept))}"
    forms = (
        (rf"(?<![\w?$]){type_pattern}\s*\.", ""),  # its own statement
        (rf"(?<![\w?$]){type_pattern}\s*;\s*", f"?{code} "),  # first in a predicate list
        (rf";\s*(?:a|rdf:type)\s+\?{re.escape(str(concept))}(?=\s*[;.])", ""),  # inside a predicate list
    )
    for form, replacement in forms:
        edits += [(match.start(), match.end(), replacement) for match in re.finditer(form, masked)]
    if len(edits) != 1:
        return None
    for match in BIND_RE.finditer(masked):
        closing = _matching_paren(masked, match.end() - 1)
        alias = re.search(r"AS\s+\?(\w+)\s*$", masked[match.end():closing], re.IGNORECASE) if closing > 0 else None
        if alias and Variable(alias.group(1)) in family:
            edits.append((match.start() + len(match.group(0)) - len(match.group(0).lstrip()), _after_dot(masked, closing + 1), ""))
    for match in FILTER_RE.finditer(masked):
        closing = _matching_paren(masked, match.end() - 1)
        read = {Variable(name) for name in re.findall(r"\?(\w+)", masked[match.end():closing])} if closing > 0 else set()
        if read and read <= family:
            edits.append((match.start(), _after_dot(masked, closing + 1), ""))
    return _apply_edits(text, edits)


def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply (start, end, replacement) edits from the last to the first; a deletion that leaves its line
    blank takes the whole line."""
    for start, end, replacement in sorted(edits, reverse=True):
        if not replacement:
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            line_end = len(text) if line_end < 0 else line_end + 1
            if not text[line_start:start].strip() and not text[end:line_end].strip():
                start, end = line_start, line_end
        text = text[:start] + replacement + text[end:]
    return text
