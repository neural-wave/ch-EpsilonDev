"""Rewrites and checks on variable scope and code-node shapes: what a clause can see, where a code sits.

The second failure analysis of the 17 October run found that a judge hint buys a whole regeneration
of the query, which comes back worse as often as better, while a deterministic rewrite, or a hint
that names the query's own variables, flips the case reliably. The shapes below have one meaning
each, so the judge repairs them without a model call:

- a sub-select hides variables that the outer SELECT, GROUP BY, ORDER BY or aggregates read
  (q-060, q-073): export them from the sub-select;
- COUNT over a variable nothing binds while the one typed record in scope is named `?drug_<it>`
  (q-068): rename;
- the object of a record link typed directly with a terminology IRI, `?intent a snomed:X`
  (q-066, q-074): hop through sphn:hasCode;
- VALUES binding a code node, the object of has*Code, to concept IRIs (q-022): type the node
  and bind the concept;
- a property path written inside an expression, `FILTER(?pid = ?r/sphn:hasSubjectPseudoIdentifier)`
  (q-069): drop it, the records already share GRAPH ?g;
- a comparison whose operand is itself a comparison, `?date <= (datediff <= 90)` (q-020): keep the
  inner one.

Two shapes get a hint that names the query's own variables instead. A BIND that reads a variable
bound outside its block: Virtuoso evaluates it on NULL, so bif:datediff fails with DT001 and any
other expression leaves the column empty (official-01, q-032; probed on dev for OPTIONAL, UNION
branches, sub-selects and later patterns). And COUNT(?guided_proc) where nothing binds the alias and
it names a typed record: the shipped hint binds the patient (q-074, q-076).

Rewrites take the text and its ParsedQuery and return (text, repairs). Each one re-parses its own
output and hands the text back untouched unless the result parses and is exactly the intended
change. Checks take (parsed, text) and return hint sentences. Every function was measured at zero
hits on the official examples, the team examples and the dev reference queries.

rdflib traps: Variable is a str subclass; a projected GROUP BY key is Extend(?key, __agg_n__) over
SAMPLE; the __agg_N__ names restart in every sub-select; `?r/sphn:p` inside an expression parses as
a MultiplicativeExpression with an IRI operand. The Virtuoso OPTION clauses are stripped before
parsing, so every text edit works on the original text with strings, IRIs and comments masked out.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.config import REPO_ROOT
from nl2sparql.context.schema import SchemaCards
from nl2sparql.judge_checks.schema import CODE_TARGETS, _Analysis, _link_targets
from nl2sparql.judge_checks.scope import (
    _expression_variables,
    _find,
    _in_scope,
    _is_projected_key,
    _select_clause,
    _triples,
    _walk,
)
from nl2sparql.judge_checks.semantics import (
    CODE_PROPERTY_RE,
    CODE_ROOTS,
    DATEDIFF,
    INFERENCE_OPTION,
    PSEUDO_IDENTIFIER,
    TERMINOLOGY_ROOTS,
    _compact,
)
from nl2sparql.sparql.parse import ParsedQuery, parse_sparql
from nl2sparql.sparql.prefixes import SPHN, STANDARD_PREFIXES

GRAPH = Variable("g")
COMPARISON_OPERATORS = {"<", ">", "<=", ">=", "=", "!="}
BOOLEAN_NODES = {"RelationalExpression", "ConditionalAndExpression", "ConditionalOrExpression", "UnaryNot"}
ARITHMETIC_NODES = {"MultiplicativeExpression", "AdditiveExpression"}
DATE_BUILTINS = {"Builtin_YEAR", "Builtin_MONTH", "Builtin_DAY", "Builtin_HOURS", "Builtin_MINUTES", "Builtin_SECONDS"}
DATE_PROPERTY_RE = re.compile(r"(?:DateTime|Date|Time)$")
TYPE_RE = r"(?:a|rdf:type)"
OPTION_RE = r"(?:\s*OPTION\s*\([^)]*\))?"
STRINGS_IRIS_COMMENTS_RE = re.compile(r"<[^<>\s]*>|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|#[^\n]*")
PREFIX_DECL_RE = re.compile(r"\bPREFIX\s+([\w-]*):\s*<([^>]+)>", re.IGNORECASE)
SUB_SELECT_RE = re.compile(r"\{\s*SELECT\b", re.IGNORECASE)
SELECT_MODIFIER_RE = re.compile(r"^\s*(?:DISTINCT|REDUCED)\b", re.IGNORECASE)
HEADER_TOKEN_RE = re.compile(r"[()]|[?$](\w+)|\*|\b(AS)\b", re.IGNORECASE)
GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
MODIFIER_RE = re.compile(r"\b(?:HAVING|ORDER\s+BY|LIMIT|OFFSET|VALUES)\b", re.IGNORECASE)
CODE_OBJECT_RE = r"(?<![\w-])(?:[\w-]*:)?(has\w*Code)\s+"
MAX_PASSES = 32  # one rewrite per pass, re-parsed each time; q-066 types one variable in twelve UNION branches


# ------------------------------------------------------------------------------------------ rewrites
def rewrite_hidden_sub_select_variables(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """Export from a sub-select the variables it binds but hides from the outer query.

    `{ SELECT ?g (COUNT(?fu) AS ?n) WHERE { ... VALUES (?c ?group_name) ... } GROUP BY ?g }` and then
    `GROUP BY ?group_name` outside: a sub-select exports only what it projects, so the outer key is
    unbound and every row lands in one empty-keyed group. The hidden variables go into the SELECT
    and the GROUP BY of the sub-select when no outer aggregate, BIND, FILTER or ORDER BY reads one
    of the sub-select's own aggregate aliases (a new group key changes their values); otherwise the
    scope check's hint stays. An aggregate alias used as its own group key is dropped from the
    GROUP BY on the way (GROUP BY ?g ?fu_count), which Virtuoso tolerates but which means nothing.
    """
    repairs: list[str] = []
    failed: set[tuple] = set()
    for _ in range(MAX_PASSES):
        if parsed.kind != "SelectQuery":
            break
        found = next(
            ((sub, hidden) for sub, hidden in _hidden_sub_selects(parsed.algebra) if (tuple(sub.PV), hidden) not in failed),
            None,
        )
        if found is None:
            break
        sub, hidden = found
        names = sorted(str(variable) for variable in hidden)
        rewritten = _export_from_sub_select(text, sub, names)
        reparsed = parse_sparql(rewritten) if rewritten is not None else None
        exported = set(sub.PV) | set(hidden)
        if reparsed is None or reparsed.kind != "SelectQuery" or not any(
            set(project.PV) == exported for project in _find(reparsed.algebra, "Project")
        ):
            failed.add((tuple(sub.PV), hidden))
            continue
        text, parsed = rewritten, reparsed
        repairs.append(
            f"Added {_join(['?' + name for name in names])} to the sub-select's SELECT and GROUP BY: the outer query "
            "reads them, and a sub-select only exports what it projects."
        )
    return text, repairs


def rewrite_count_of_misnamed_record(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """COUNT(?prescription) where nothing binds ?prescription and the one typed record in scope is
    ?drug_prescription: the count was meant for that record, so rename the variable.

    Only a variable that nothing binds anywhere and that appears in COUNT() alone is renamed: a
    variable passed to IF() may be unbound on purpose (official-04's reference).
    """
    if parsed.kind != "SelectQuery":
        return text, []
    bound_anywhere = _bound_anywhere(parsed.algebra)
    renames: dict[str, tuple[str, str]] = {}
    for project in _find(parsed.algebra, "Project"):
        extends, aggregate_join = _select_clause(project)
        if aggregate_join is None:
            continue
        aliases = {extend.var for extend in extends}
        in_scope = _in_scope(aggregate_join.p)
        records = _typed_records(aggregate_join.p)
        for aggregate in aggregate_join.A:
            counted = aggregate.vars
            if aggregate.name != "Aggregate_Count" or not isinstance(counted, Variable):
                continue
            if counted in in_scope or counted in aliases or counted in bound_anywhere or str(counted) in renames:
                continue
            candidates = [record for record in records if record in in_scope and str(record).endswith("_" + str(counted))]
            if len(candidates) == 1 and _only_counted(text, str(counted)):
                renames[str(counted)] = (str(candidates[0]), records[candidates[0]])
    if not renames:
        return text, []
    rewritten = text
    for old, (new, _cls) in renames.items():
        rewritten = re.sub(_variable_re(old), f"?{new}", rewritten)
    if parse_sparql(rewritten).kind != "SelectQuery":
        return text, []
    return rewritten, [
        f"Renamed ?{old} to ?{new} in COUNT(?{old}): nothing binds ?{old}, and ?{new} is the sphn:{cls} record in scope."
        for old, (new, cls) in renames.items()
    ]


def rewrite_record_link_typed_with_code(
    text: str, parsed: ParsedQuery, schema: SchemaCards | None = None,
) -> tuple[str, list[str]]:
    """'?proc sphn:hasIntent ?intent . ?intent a snomed:X': the object of a record link typed directly
    with a terminology IRI. The cards say the link points at one record class that carries its code
    on sphn:hasCode, so the type belongs to the code node: ?intent sphn:hasCode ?intent_code .
    ?intent_code a snomed:X (the inference option, when written, moves with the type)."""
    schema = _cards() if schema is None else schema
    repairs: list[str] = []
    failed: set[tuple] = set()
    for _ in range(MAX_PASSES):
        if schema.empty or parsed.kind != "SelectQuery":
            break
        candidate = next((link for link in _concrete_typed_links(parsed.algebra, schema) if link not in failed), None)
        if candidate is None:
            break
        variable, iri, prop, target = candidate
        rewritten = _hop_through_code(text, str(variable), str(iri))
        reparsed = parse_sparql(rewritten) if rewritten is not None else None
        if reparsed is None or reparsed.kind != "SelectQuery" or not _hopped(parsed, reparsed, variable, iri):
            failed.add(candidate)
            continue
        text, parsed = rewritten, reparsed
        repair = (
            f"Wrote ?{variable} sphn:hasCode ?{variable}_code . ?{variable}_code a {_compact(iri)} instead of "
            f"'?{variable} a {_compact(iri)}': ?{variable} is a sphn:{target} record (object of sphn:{prop}), "
            "and its code sits on sphn:hasCode."
        )
        if repair not in repairs:
            repairs.append(repair)
    return text, repairs


def rewrite_values_on_code_node(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """VALUES (?code ?label) { (snomed:X "x") ... } where ?code is the object of has*Code and has no
    type triple: a code node is one IRI per record and never equal to a concept, so nothing matches.
    Type the node, ?code a ?code_concept (with the inference option for ICD-10-GM and CHOP), and bind
    the concept instead, which is what the VALUES meant; every other use of ?code follows."""
    repairs: list[str] = []
    failed: set[Variable] = set()
    for _ in range(MAX_PASSES):
        if parsed.kind != "SelectQuery":
            break
        candidate = next((item for item in _values_on_code_nodes(parsed.algebra) if item[0] not in failed), None)
        if candidate is None:
            break
        variable, prop, hierarchical = candidate
        rewritten = _type_code_node(text, str(variable), hierarchical)
        reparsed = parse_sparql(rewritten) if rewritten is not None else None
        if reparsed is None or reparsed.kind != "SelectQuery" or not _concept_bound(reparsed, variable):
            failed.add(variable)
            continue
        text, parsed = rewritten, reparsed
        option = " with the inference option" if hierarchical else ""
        repairs.append(
            f"VALUES bound ?{variable}, the code node of sphn:{prop}, to concept IRIs, which a code node never "
            f"equals: added ?{variable} a ?{variable}_concept{option} and moved the VALUES to ?{variable}_concept."
        )
    return text, repairs


def rewrite_path_in_filter(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """FILTER(?pid = ?r/sphn:hasSubjectPseudoIdentifier): inside an expression '/' is a division, so
    the filter never holds. When both records stand in GRAPH ?g they belong to the same patient
    already: the FILTER and the ?pid triple go, and nothing else changes."""
    repairs: list[str] = []
    failed: set[tuple] = set()
    for _ in range(MAX_PASSES):
        if parsed.kind != "SelectQuery":
            break
        candidate = next((item for item in _pseudo_identifier_path_filters(parsed.algebra) if item not in failed), None)
        if candidate is None:
            break
        pid, record, subject = candidate
        rewritten = _drop_path_filter(text, str(pid), str(record), str(subject))
        reparsed = parse_sparql(rewritten) if rewritten is not None else None
        dropped = reparsed is not None and reparsed.kind == "SelectQuery" and _dropped(parsed, reparsed, (subject, PSEUDO_IDENTIFIER, pid))
        if not dropped:
            failed.add(candidate)
            continue
        text, parsed = rewritten, reparsed
        repairs.append(
            f"Removed FILTER(?{pid} = ?{record}/sphn:hasSubjectPseudoIdentifier) and the ?{pid} triple: '/' is a "
            f"division inside an expression, not a path, and ?{subject} and ?{record} are already in the same "
            "GRAPH ?g (one patient)."
        )
    return text, repairs


def rewrite_nested_comparison(text: str, parsed: ParsedQuery) -> tuple[str, list[str]]:
    """?rt_start <= (bif:datediff("day", ?surgery_start, ?rt_start) <= 90): the outer comparison tests a
    date against true/false. When the inner comparison is a datediff over that very date, the inner
    one is what was meant and the outer one goes."""
    repairs: list[str] = []
    failed: set[tuple] = set()
    for _ in range(MAX_PASSES):
        if parsed.kind != "SelectQuery":
            break
        candidate = next((item for item in _date_against_datediff(parsed.algebra) if item not in failed), None)
        if candidate is None:
            break
        name, operator = candidate
        rewritten = _drop_outer_comparison(text, name, operator)
        reparsed = parse_sparql(rewritten) if rewritten is not None else None
        if reparsed is None or reparsed.kind != "SelectQuery" or len(_nested_comparisons(reparsed.algebra)) >= len(
            _nested_comparisons(parsed.algebra)
        ):
            failed.add(candidate)
            continue
        text, parsed = rewritten, reparsed
        repairs.append(
            f"Replaced ?{name} {operator} (<comparison>) by the inner comparison: a comparison yields true or "
            f"false, which cannot be compared with the date ?{name}."
        )
    return text, repairs


# -------------------------------------------------------------------------------------------- checks
def check_bind_out_of_scope(parsed: ParsedQuery, text: str) -> list[str]:
    """A BIND whose expression needs a variable that is not in scope where the BIND stands.

    A BIND sees only the patterns before it in its own block: inside OPTIONAL { } the variables of
    the outer block are invisible, after a sub-select only its projection is, and a UNION branch or
    a later pattern binds nothing for it. Virtuoso then evaluates the expression on NULL: bif:datediff
    and the date functions fail with DT001, anything else leaves the column empty. Variables inside
    IF() result arms, COALESCE(), BOUND() and EXISTS are read on purpose and left alone.
    """
    if parsed.kind != "SelectQuery":
        return []
    bound_anywhere = _bound_anywhere(parsed.algebra)
    hints: list[str] = []

    def visit(node, inside_optional: bool, graphs: frozenset) -> None:
        if isinstance(node, (list, tuple)):
            for item in node:
                visit(item, inside_optional, graphs)
            return
        if not isinstance(node, CompValue):
            return
        if node.name == "Extend" and not str(node.var).startswith("__agg") and not _is_projected_key(node):
            in_scope = _in_scope(node.p) | graphs  # inside GRAPH ?g { ... } the graph variable is bound
            missing = sorted(
                variable for variable in _read_variables(node.expr)
                if not str(variable).startswith("__agg") and variable not in in_scope and variable in bound_anywhere
            )
            if missing:
                hints.append(_out_of_scope_hint(node, missing, inside_optional, in_scope, parsed.algebra))
        if node.name == "Graph" and isinstance(node.term, Variable):
            graphs = graphs | {node.term}
        for key, value in node.items():
            if not key.startswith("_"):
                visit(value, inside_optional or (node.name == "LeftJoin" and key == "p2"), graphs)

    visit(parsed.algebra, False, frozenset())
    return list(dict.fromkeys(hints))


def check_nested_comparison(parsed: ParsedQuery, text: str) -> list[str]:
    """A comparison whose operand is itself a comparison, left after the rewrite could not resolve it."""
    if parsed.kind != "SelectQuery":
        return []
    hints = []
    for outer, operand, inner in _nested_comparisons(parsed.algebra):
        kind = "date" if isinstance(operand, Variable) and _is_date_variable(operand, parsed.algebra) else "value"
        hints.append(
            f"FILTER: {_render_expression(outer)} compares a {kind} with true/false, because "
            f"({_render_expression(inner)}) is itself a comparison, and Virtuoso rejects it (SP031). Write each "
            'condition on its own, joined with &&: FILTER(?a > ?b && bif:datediff("day", ?b, ?a) <= 90).'
        )
    return list(dict.fromkeys(hints))


def check_path_in_expression(parsed: ParsedQuery, text: str) -> list[str]:
    """?r/sphn:p inside an expression, left after the rewrite could not drop it: '/' divides."""
    if parsed.kind != "SelectQuery":
        return []
    hints = []
    for node in _find(parsed.algebra, "MultiplicativeExpression") + _find(parsed.algebra, "AdditiveExpression"):
        operands = [node.expr] + (list(node.other) if isinstance(node.other, list) else [node.other])
        iris = [operand for operand in operands if isinstance(operand, URIRef)]
        if not iris:
            continue
        subject = next((operand for operand in operands if isinstance(operand, Variable)), None)
        iri = iris[0]
        value = f"?{subject}_{_snake(iri)}" if subject is not None else "?value"
        hint = (
            f"{_render_expression(node)} inside an expression is a division, not a property path: bind the value "
            f"with a triple pattern, {f'?{subject}' if subject is not None else '?x'} {_compact(iri)} {value}, "
            f"and compare {value}."
        )
        if iri == PSEUDO_IDENTIFIER:
            hint += " Records of the same patient are already in the same GRAPH ?g: match the second record there."
        hints.append(hint)
    return list(dict.fromkeys(hints))


def record_subgroup_hint(project, variable: Variable) -> str | None:
    """The hint for COUNT(?guided_proc) when nothing binds the alias and it ends with the name of a
    typed record (?proc): the column counts records, so the record is what to bind, not the patient.
    None when no single typed record is named, and the patient form stays.

    scope.py's unbound check builds the same remedy for this suffix case from the records of the
    SELECT's own scope; this one also sees a record typed inside a sub-select, which is where q-074's
    first try types ?proc while it counts ?guided_proc outside."""
    records = [record for record in _typed_records(project.p, nested=True) if str(variable).endswith("_" + str(record))]
    if len(records) != 1:
        return None
    record = records[0]
    return (
        f"COUNT(?{variable}): nothing binds ?{variable}. It counts ?{record} records, so bind the record: "
        f"OPTIONAL {{ GRAPH ?g {{ <condition on ?{record}> }} BIND(?{record} AS ?{variable}) }} "
        f"and COUNT(DISTINCT ?{variable})."
    )


def check_unbound_count_records(parsed: ParsedQuery, text: str) -> list[str]:
    """record_subgroup_hint for every COUNT over a variable that nothing binds, as scope.py's unbound
    check sees them. Not in CHECKS: the judge gets this hint through scope.py, which replaces its
    patient form by this one; the function here serves the tests and the measurements."""
    if parsed.kind != "SelectQuery":
        return []
    hints = []
    for project in _find(parsed.algebra, "Project"):
        extends, aggregate_join = _select_clause(project)
        if aggregate_join is None:
            continue
        bound = _in_scope(aggregate_join.p) | {extend.var for extend in extends}
        nested = set()
        for sub_select in _find(project.p, "Project"):
            nested |= _expression_variables(sub_select)
        for aggregate in aggregate_join.A:
            counted = aggregate.vars
            if aggregate.name != "Aggregate_Count" or not isinstance(counted, Variable) or counted in bound or counted in nested:
                continue
            hint = record_subgroup_hint(project, counted)
            if hint:
                hints.append(hint)
    return list(dict.fromkeys(hints))


REWRITES = [
    rewrite_hidden_sub_select_variables,
    rewrite_count_of_misnamed_record,
    rewrite_record_link_typed_with_code,
    rewrite_values_on_code_node,
    rewrite_path_in_filter,
    rewrite_nested_comparison,
]
CHECKS = [check_bind_out_of_scope, check_nested_comparison, check_path_in_expression]


# ------------------------------------------------------------------------------- hidden sub-select
def _hidden_sub_selects(algebra):
    """(sub-select, hidden variables) for every sub-select that the rewrite may export from."""
    for project in _find(algebra, "Project"):
        for sub in _direct_sub_selects(project):
            inside = {variable for variable in _in_scope(sub.p) if not str(variable).startswith("__agg")}
            hidden = (inside - set(sub.PV)) & _outer_uses(project, sub)
            hidden.discard(GRAPH)
            if hidden and not _outer_reads_aliases(project, sub):
                yield sub, frozenset(hidden)


def _direct_sub_selects(project) -> list[CompValue]:
    found = []

    def visit(node) -> bool:
        if node is not project and node.name == "Project":
            found.append(node)
            return True
        return False

    _walk(project.p, visit)
    return found


def _outer_uses(project, sub) -> set[Variable]:
    """What the outer scope reads in its SELECT, GROUP BY, ORDER BY and aggregates, less what it binds itself."""
    used = set(project.PV)
    bound: set[Variable] = set()

    def visit(node) -> bool:
        if node is sub:
            return True
        if node.name == "Project":
            bound.update(node.PV)  # a sibling sub-select binds what it projects
            return True
        if node.name in ("Group", "OrderBy"):
            used.update(_expression_variables(node.expr))
        elif node.name == "AggregateJoin":
            for aggregate in node.A:
                if aggregate.name != "Aggregate_Sample":
                    used.update(_expression_variables(aggregate.vars))
        elif node.name == "BGP":
            bound.update(term for triple in node.triples for term in triple if isinstance(term, Variable))
        elif node.name == "Extend" and not str(node.var).startswith("__agg") and not _is_projected_key(node):
            bound.add(node.var)
        elif node.name == "values":
            for row in node.res:
                bound.update(row)
        return False

    _walk(project.p, visit)
    return used - bound


def _outer_reads_aliases(project, sub) -> bool:
    """True when an outer aggregate, BIND, FILTER, HAVING or ORDER BY reads one of the sub-select's aggregate aliases."""
    extends, _ = _select_clause(sub)
    aliases = {extend.var for extend in extends if not _is_projected_key(extend)} & set(sub.PV)
    if not aliases:
        return False
    read: set[Variable] = set()

    def visit(node) -> bool:
        if node is sub or node.name == "Project":
            return True
        if node.name in ("Extend", "Filter", "OrderBy", "LeftJoin") and node.get("expr") is not None:
            read.update(_expression_variables(node.expr))
        elif node.name == "AggregateJoin":
            for aggregate in node.A:
                if aggregate.name != "Aggregate_Sample":
                    read.update(_expression_variables(aggregate.vars))
        return False

    _walk(project.p, visit)
    return bool(read & aliases)


def _export_from_sub_select(text: str, sub, names: list[str]) -> str | None:
    """The text with the names added to the sub-select's SELECT and GROUP BY; None when the sub-select
    cannot be told apart in the text."""
    masked = _masked(text)
    wanted = {str(variable) for variable in sub.PV}
    spans = [span for span in _sub_select_spans(masked) if span.projected is not None and set(span.projected) == wanted]
    if len(spans) != 1:
        return None
    span = spans[0]
    extends, aggregate_join = _select_clause(sub)
    aliases = {str(extend.var) for extend in extends if not _is_projected_key(extend)}
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement), applied from the end
    modifiers = masked[span.where_close + 1:span.close]
    group_by = GROUP_BY_RE.search(modifiers)
    if group_by is not None:
        keys_start = span.where_close + 1 + group_by.end()
        following = MODIFIER_RE.search(modifiers, group_by.end())
        keys_end = span.where_close + 1 + (following.start() if following else len(modifiers))
        keys = text[keys_start:keys_end]
        kept = keys.strip()
        if "(" not in kept:  # plain keys: drop the sub-select's own aggregate aliases
            kept = " ".join(token for token in kept.split() if token.lstrip("?$") not in aliases)
        new_keys = " ".join(([kept] if kept else []) + [f"?{name}" for name in names])
        edits.append((keys_start, keys_end, " " + new_keys + keys[len(keys.rstrip()):]))
    elif aggregate_join is not None:
        edits.append((span.where_close + 1, span.where_close + 1, " GROUP BY " + " ".join(f"?{name}" for name in names)))
    header = masked[span.header_start:span.where_open]
    modifier = SELECT_MODIFIER_RE.match(header)
    plain = re.match(r"(?:\s+[?$]\w+)+", header[modifier.end() if modifier else 0:])  # after the plain variables
    position = span.header_start + (modifier.end() if modifier else 0) + (plain.end() if plain else 0)
    edits.append((position, position, " " + " ".join(f"?{name}" for name in names)))
    return _apply(text, edits)


@dataclass(frozen=True)
class _Span:
    """Where a sub-select sits in the masked text: its braces, its header and its WHERE block."""

    open: int
    header_start: int
    where_open: int
    where_close: int
    close: int
    projected: list[str] | None  # the names its header projects; None for SELECT *


def _sub_select_spans(masked: str) -> list[_Span]:
    spans = []
    for match in SUB_SELECT_RE.finditer(masked):
        close = _matching(masked, match.start(), "{", "}")
        where_open = masked.find("{", match.end())
        if close is None or where_open < 0 or where_open > close:
            continue
        where_close = _matching(masked, where_open, "{", "}")
        if where_close is None or where_close > close:
            continue
        projected = _projected_names(masked[match.end():where_open])
        spans.append(_Span(match.start(), match.end(), where_open, where_close, close, projected))
    return spans


def _projected_names(header: str) -> list[str] | None:
    """The variables a SELECT header projects: plain ?x at depth 0 and every (... AS ?x); None for SELECT *."""
    names, depth, after_as = [], 0, False
    for match in HEADER_TOKEN_RE.finditer(header):
        token = match.group(0)
        if token == "*":
            return None
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif match.group(1):
            if depth == 0 or after_as:
                names.append(match.group(1))
        after_as = token.upper() == "AS"
    return names


# --------------------------------------------------------------------------------- misnamed record
def _only_counted(text: str, name: str) -> bool:
    """True when every occurrence of ?name in the query is the argument of a COUNT()."""
    masked = _masked(text)
    occurrences = len(re.findall(_variable_re(name), masked))
    counted = len(re.findall(rf"\bCOUNT\s*\(\s*(?:DISTINCT\s+)?{_variable_re(name)}\s*\)", masked, re.IGNORECASE))
    return occurrences > 0 and occurrences == counted


# ------------------------------------------------------------------------------ record link typed
@lru_cache(maxsize=1)
def _cards() -> SchemaCards:
    return SchemaCards.load(REPO_ROOT / "data" / "schema_cards.json")


def _concrete_typed_links(algebra, schema: SchemaCards) -> list[tuple]:
    """(variable, terminology IRI, link property, target class) for '?x sphn:link ?y . ?y a <code>'."""
    analysis = _Analysis(algebra, schema)
    found = []
    for pattern, prop in analysis.links():
        if prop.endswith("Code") or not isinstance(pattern.object, Variable):
            continue
        targets = _link_targets(prop, schema)
        if len(targets) != 1:
            continue
        target = next(iter(targets))
        if target in CODE_TARGETS or target not in schema.classes or "hasCode" not in schema.classes[target]["properties"]:
            continue
        for typed in analysis.types.get(pattern.object, ()):
            if typed.joins(pattern) and isinstance(typed.object, URIRef) and str(typed.object).startswith(TERMINOLOGY_ROOTS):
                item = (pattern.object, typed.object, prop, target)
                if item not in found:
                    found.append(item)
    return found


def _hop_through_code(text: str, name: str, iri: str) -> str | None:
    """'?y a X' -> '?y sphn:hasCode ?y_code . ?y_code a X' for the first statement that fits a known form."""
    code = f"{name}_code"
    masked = _masked(text)
    variable, forms = _variable_re(name), "|".join(_lexical_forms(text, iri))
    uses = len(re.findall(_variable_re(code), masked))
    hops = len(re.findall(rf"{variable}\s+sphn:hasCode\s+{_variable_re(code)}\s*\.\s*{_variable_re(code)}\s+{TYPE_RE}\s", masked))
    if uses != 2 * hops:
        return None  # ?y_code already means something else in this query
    cls = rf"(?P<cls>{forms})(?P<option>{OPTION_RE})"
    typed = rf"{variable}\s+{TYPE_RE}\s+{cls}"
    hop = rf"?{name} sphn:hasCode ?{code} . ?{code} a \g<cls>\g<option>"
    attempts = (  # (where the statement stands, in the masked text; the statement itself; its replacement)
        (rf"{typed}\s*\.", None, hop + " ."),  # a statement of its own
        (rf"{typed}\s*;\s*", None, rf"?{code} a \g<cls>\g<option> . ?{name} sphn:hasCode ?{code} ; "),  # the head of a ; chain
        (rf";\s*{TYPE_RE}\s+{cls}\s*\.", None, rf"; sphn:hasCode ?{code} . ?{code} a \g<cls>\g<option> ."),  # the tail of a chain
        (rf"{typed}(?=\s*\}})", typed, hop),  # the last statement of a group, without a dot
    )
    for where, statement, replacement in attempts:
        for match in re.finditer(where, masked):
            start, end = match.span()
            # The groups come from the original text: the mask blanks the string inside OPTION (inference "...").
            original = re.match(statement or where, text[start:end])
            if original is not None:
                return text[:start] + original.expand(replacement) + text[end:]
    return None


def _hopped(parsed: ParsedQuery, reparsed: ParsedQuery, variable: Variable, iri: URIRef) -> bool:
    """True when the rewrite replaced one '?y a X' by the hasCode hop and changed nothing else."""
    code = Variable(f"{variable}_code")
    before = Counter(_all_triples(parsed.algebra))
    before[(variable, RDF.type, iri)] -= 1
    before[(variable, URIRef(SPHN + "hasCode"), code)] += 1
    before[(code, RDF.type, iri)] += 1
    return +before == Counter(_all_triples(reparsed.algebra))


# ------------------------------------------------------------------------------ VALUES on a code node
def _values_on_code_nodes(algebra) -> list[tuple[Variable, str, bool]]:
    """(code-node variable, its has*Code property, ICD-10-GM/CHOP among the values) for every VALUES that
    binds an untyped code node to terminology IRIs."""
    code_nodes: dict[Variable, str] = {}
    typed = set()
    for subject, predicate, obj in _all_triples(algebra):
        if isinstance(predicate, URIRef) and str(predicate).startswith(SPHN) and isinstance(obj, Variable):
            if CODE_PROPERTY_RE.match(str(predicate)[len(SPHN):]):
                code_nodes.setdefault(obj, str(predicate)[len(SPHN):])
        if predicate == RDF.type and isinstance(subject, Variable):
            typed.add(subject)
    found: dict[Variable, tuple[str, bool]] = {}
    for values in _find(algebra, "values"):
        for row in values.res:
            for variable, value in row.items():
                if variable not in code_nodes or variable in typed:
                    continue
                if isinstance(value, URIRef) and str(value).startswith(TERMINOLOGY_ROOTS):
                    prop, hierarchical = found.get(variable, (code_nodes[variable], False))
                    found[variable] = (prop, hierarchical or str(value).startswith(CODE_ROOTS))
    return [(variable, prop, hierarchical) for variable, (prop, hierarchical) in found.items()]


def _type_code_node(text: str, name: str, hierarchical: bool) -> str | None:
    """Insert '?name a ?name_concept' after the has*Code statement and use ?name_concept everywhere else."""
    concept = f"{name}_concept"
    masked = _masked(text)
    if re.search(_variable_re(concept), masked):
        return None
    objects = list(re.finditer(CODE_OBJECT_RE + _variable_re(name), masked))
    if not objects:
        return None
    keep = {match.end() - len(name) - 1 for match in objects}  # the ?name tokens that stay the code node
    end = _statement_end(masked, objects[0].end())
    if end is None:
        return None
    option = f" {INFERENCE_OPTION}" if hierarchical else ""
    position, closing = end
    triple = f"?{name} a ?{concept}{option}"
    if closing:  # the statement ends at the group's brace: finish it with a dot before the new triple
        position = len(masked[:position].rstrip())
        edits = [(position, position, f" . {triple}")]
    else:
        edits = [(position + 1, position + 1, f" {triple} .")]
    for match in re.finditer(_variable_re(name), masked):
        if match.start() not in keep:
            edits.append((match.start(), match.end(), f"?{concept}"))
    return _apply(text, edits)


def _statement_end(masked: str, position: int) -> tuple[int, bool] | None:
    """The index of the '.' that ends the statement at position, or of the '}' that closes its group
    (closing=True); None when a nested block starts first."""
    depth = 0
    for index in range(position, len(masked)):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char == ".":
            return index, False
        elif depth == 0 and char == "}":
            return index, True
        elif depth == 0 and char == "{":
            return None
    return None


def _concept_bound(reparsed: ParsedQuery, variable: Variable) -> bool:
    concept = Variable(f"{variable}_concept")
    triples = _all_triples(reparsed.algebra)
    typed = (variable, RDF.type, concept) in triples
    still_object = any(isinstance(predicate, URIRef) and obj == variable for _, predicate, obj in triples)
    values_vars = {var for values in _find(reparsed.algebra, "values") for row in values.res for var in row}
    return typed and still_object and concept in values_vars and variable not in values_vars


# ------------------------------------------------------------------------------- path in a filter
def _pseudo_identifier_path_filters(algebra) -> list[tuple[Variable, Variable, Variable]]:
    """(pid, record, subject) for FILTER(?pid = ?record/sphn:hasSubjectPseudoIdentifier) with the ?pid
    triple on ?subject, when ?record and ?subject both stand in GRAPH ?g."""
    graphed = _graph_variables(algebra)
    pid_triples = {
        obj: subject for subject, predicate, obj in _all_triples(algebra)
        if predicate == PSEUDO_IDENTIFIER and isinstance(obj, Variable)
    }
    found = []
    for comparison in _find(algebra, "RelationalExpression"):
        if comparison.op != "=":
            continue
        for pid, path in ((comparison.expr, comparison.other), (comparison.other, comparison.expr)):
            record = _path_subject(path, PSEUDO_IDENTIFIER)
            if isinstance(pid, Variable) and record is not None and pid in pid_triples:
                subject = pid_triples[pid]
                if record in graphed and subject in graphed and (pid, record, subject) not in found:
                    found.append((pid, record, subject))
    return found


def _path_subject(node, iri: URIRef) -> Variable | None:
    """?r for the expression '?r / <iri>'."""
    if not (isinstance(node, CompValue) and node.name == "MultiplicativeExpression" and isinstance(node.expr, Variable)):
        return None
    others = list(node.other) if isinstance(node.other, list) else [node.other]
    operators = list(node.op) if isinstance(node.op, list) else [node.op]
    return node.expr if operators == ["/"] and others == [iri] else None


def _graph_variables(algebra) -> set[Variable]:
    """Variables of the triples inside GRAPH ?g blocks."""
    found: set[Variable] = set()
    for graph in _find(algebra, "Graph"):
        if graph.term == GRAPH:
            found.update(term for triple in _all_triples(graph.p) for term in triple if isinstance(term, Variable))
    return found


def _drop_path_filter(text: str, pid: str, record: str, subject: str) -> str | None:
    pid_re, record_re, subject_re = _variable_re(pid), _variable_re(record), _variable_re(subject)
    forms = "|".join(_lexical_forms(text, str(PSEUDO_IDENTIFIER)))
    path = rf"{record_re}\s*/\s*(?:{forms})"
    filters = re.compile(rf"\s*\bFILTER\s*\(\s*(?:{pid_re}\s*=\s*{path}|{path}\s*=\s*{pid_re})\s*\)", re.IGNORECASE)
    without_filter, count = filters.subn("", text, count=1)
    if count != 1:
        return None
    predicate = rf"(?:{forms})"
    attempts = (
        (rf"{subject_re}\s+{predicate}\s+{pid_re}\s*\.", ""),  # a statement of its own
        (rf"{subject_re}\s+{predicate}\s+{pid_re}\s*;\s*", f"?{subject} "),  # the head of a ; chain
        (rf"\s*;\s*{predicate}\s+{pid_re}", ""),  # a later part of the chain
        (rf"{subject_re}\s+{predicate}\s+{pid_re}(?=\s*\}})", ""),  # the last statement of a group
    )
    for pattern, replacement in attempts:
        rewritten, count = re.subn(pattern, replacement, without_filter, count=1)
        if count and not re.search(pid_re, _masked(rewritten)):
            return rewritten
    return None


def _dropped(parsed: ParsedQuery, reparsed: ParsedQuery, triple: tuple) -> bool:
    before = Counter(_all_triples(parsed.algebra))
    before[triple] -= 1
    return +before == Counter(_all_triples(reparsed.algebra))


# ----------------------------------------------------------------------------- nested comparison
def _nested_comparisons(algebra) -> list[tuple[CompValue, object, CompValue]]:
    """(outer comparison, its other operand, the inner comparison) wherever a comparison compares a comparison."""
    found = []
    for outer in _find(algebra, "RelationalExpression"):
        if outer.op not in COMPARISON_OPERATORS:
            continue
        for operand, inner in ((outer.expr, outer.other), (outer.other, outer.expr)):
            if isinstance(inner, CompValue) and inner.name in BOOLEAN_NODES:
                found.append((outer, operand, inner))
                break
    return found


def _date_against_datediff(algebra) -> list[tuple[str, str]]:
    """(date variable, operator) for '?date op (bif:datediff(..., ?date, ...) op2 n)'."""
    found = []
    for outer, operand, inner in _nested_comparisons(algebra):
        if isinstance(operand, Variable) and inner.name == "RelationalExpression" and operand in _datediff_arguments(inner):
            if (str(operand), outer.op) not in found:
                found.append((str(operand), outer.op))
    return found


def _drop_outer_comparison(text: str, name: str, operator: str) -> str | None:
    """'?name op ( inner )' or '( inner ) op ?name' -> '( inner )' for the first bracket that holds a datediff on ?name."""
    masked = _masked(text)
    variable, op = _variable_re(name), re.escape(operator)
    for match in re.finditer(rf"{variable}\s*{op}\s*\(", masked):
        close = _matching(masked, match.end() - 1, "(", ")")
        if close is not None and _datediff_on(masked[match.end():close], name):
            return text[:match.start()] + text[match.end() - 1:]
    for match in re.finditer(rf"\)\s*{op}\s*{variable}", masked):
        open_index = _matching_open(masked, match.start(), "(", ")")
        if open_index is not None and _datediff_on(masked[open_index + 1:match.start()], name):
            return text[:match.start() + 1] + text[match.end():]
    return None


def _datediff_on(inner: str, name: str) -> bool:
    return re.search(r"\bdatediff\s*\(", inner, re.IGNORECASE) is not None and re.search(_variable_re(name), inner) is not None


def _datediff_arguments(expression) -> set[Variable]:
    found: set[Variable] = set()

    def visit(node) -> bool:
        if node.name == "Function" and node.iri == DATEDIFF:
            found.update(argument for argument in node.expr if isinstance(argument, Variable))
        return False

    _walk(expression, visit)
    return found


def _is_date_variable(variable: Variable, algebra) -> bool:
    """A datediff argument, or the object of a date property (hasStartDateTime, hasDate)."""
    if variable in _datediff_arguments(algebra):
        return True
    return any(
        obj == variable and isinstance(predicate, URIRef) and DATE_PROPERTY_RE.search(str(predicate)) is not None
        for _, predicate, obj in _all_triples(algebra)
    )


# -------------------------------------------------------------------------------- BIND out of scope
def _read_variables(expression) -> set[Variable]:
    """Variables an expression needs a value for: not IF() result arms, COALESCE(), BOUND() or EXISTS patterns."""
    found: set[Variable] = set()

    def visit(node) -> None:
        if isinstance(node, Variable):
            found.add(node)
        elif isinstance(node, (list, tuple)):
            for item in node:
                visit(item)
        elif isinstance(node, CompValue):
            if node.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS", "Builtin_BOUND", "Builtin_COALESCE"):
                return
            if node.name == "Builtin_IF":
                visit(node.arg1)
                return
            for key, value in node.items():
                if not key.startswith("_"):
                    visit(value)

    visit(expression)
    return found


def _out_of_scope_hint(extend, missing: list[Variable], inside_optional: bool, in_scope: set[Variable], algebra) -> str:
    named = _join([f"?{variable}" for variable in missing])
    failing = _failing_function(extend.expr, set(missing))
    outcome = f"{failing} fails (DT001)" if failing else f"?{extend.var} stays empty"
    hidden_by = [  # a sub-select that binds the variable without projecting it, the BIND standing outside it
        sub for sub in _find(algebra, "Project")
        if not any(node is extend for node in _find(sub.p, "Extend"))
        and any(variable in _in_scope(sub.p) and variable not in sub.PV for variable in missing)
    ]
    if hidden_by:
        return (
            f"BIND(... AS ?{extend.var}) cannot see {named}: the sub-select binds {'them' if len(missing) > 1 else 'it'} "
            f"but exports only what it projects, so {outcome}. Add {' '.join(f'?{variable}' for variable in missing)} "
            f"to the sub-select's SELECT and GROUP BY, or compute ?{extend.var} inside it."
        )
    if inside_optional:
        guards = sorted(
            variable for variable in _read_variables(extend.expr) if variable in in_scope and not str(variable).startswith("__agg")
        )
        expression = _render_expression(extend.expr)
        bound = " && ".join(f"BOUND(?{variable})" for variable in guards)
        guarded = f"IF({bound}, {expression}, ?none)" if guards else expression
        return (
            f"BIND(... AS ?{extend.var}) inside OPTIONAL cannot see {named} (bound outside it): {outcome}. "
            f"Compute it after the OPTIONAL: BIND({guarded} AS ?{extend.var})"
        )
    return (
        f"BIND(... AS ?{extend.var}) cannot see {named}: {'they are' if len(missing) > 1 else 'it is'} bound in a later "
        f"pattern or in another block (an OPTIONAL or a UNION branch), and a BIND sees only the patterns before it "
        f"in its own block, so {outcome}. Move the BIND after the pattern that binds {named}."
    )


def _failing_function(expression, variables: set[Variable]) -> str | None:
    """The date function that reads one of the variables: it raises DT001 on NULL."""
    found: list[str] = []

    def visit(node) -> bool:
        if node.name == "Function" and node.iri == DATEDIFF and any(argument in variables for argument in node.expr):
            found.append("bif:datediff")
        elif node.name in DATE_BUILTINS and isinstance(node.arg, Variable) and node.arg in variables:
            found.append(f"{node.name[len('Builtin_'):]}()")
        return False

    _walk(expression, visit)
    return found[0] if found else None


# ---------------------------------------------------------------------------------------- helpers
def _masked(text: str) -> str:
    """Same length as text, with strings, IRIs and comments blanked, so positions stay valid."""
    return STRINGS_IRIS_COMMENTS_RE.sub(lambda match: " " * len(match.group(0)), text)


def _matching(masked: str, open_index: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(open_index, len(masked)):
        if masked[index] == opening:
            depth += 1
        elif masked[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _matching_open(masked: str, close_index: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(close_index, -1, -1):
        if masked[index] == closing:
            depth += 1
        elif masked[index] == opening:
            depth -= 1
            if depth == 0:
                return index
    return None


def _apply(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Replace the (start, end) spans by their replacements, from the end so earlier positions hold."""
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _variable_re(name: str) -> str:
    return rf"[?$]{re.escape(name)}(?!\w)"


def _lexical_forms(text: str, iri: str) -> list[str]:
    """Every way the query may write the IRI: <iri> and prefix:local for each declared or standard prefix."""
    namespaces = {namespace: prefix for prefix, namespace in STANDARD_PREFIXES.items() if prefix != "bif"}
    namespaces.update({namespace: prefix for prefix, namespace in PREFIX_DECL_RE.findall(text)})
    forms = [re.escape(f"<{iri}>")]
    for namespace, prefix in namespaces.items():
        if iri.startswith(namespace):
            forms.append(re.escape(f"{prefix}:{iri[len(namespace):]}"))
    return forms


def _all_triples(algebra) -> list[tuple]:
    """Every BGP triple of the query, sub-selects included."""
    found = []

    def visit(node) -> bool:
        if node.name == "BGP":
            found.extend(tuple(triple) for triple in node.triples)
        return False

    _walk(algebra, visit)
    return found


def _bound_anywhere(algebra) -> set[Variable]:
    """Every variable some pattern, BIND, VALUES or GRAPH binds, in any scope of the query."""
    found: set[Variable] = set()

    def visit(node) -> bool:
        if node.name == "BGP":
            found.update(term for triple in node.triples for term in triple if isinstance(term, Variable))
        elif node.name == "Extend" and not str(node.var).startswith("__agg"):
            found.add(node.var)
        elif node.name == "values":
            for row in node.res:
                found.update(row)
        elif isinstance(node.get("term"), Variable):
            found.add(node.term)
        return False

    _walk(algebra, visit)
    return found


def _typed_records(pattern, nested: bool = False) -> dict[Variable, str]:
    """Subject -> class name for every '?x a sphn:Class' in the pattern, sub-selects excluded unless nested."""
    records: dict[Variable, str] = {}
    for subject, predicate, obj in (_all_triples(pattern) if nested else _triples(pattern)):
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(obj, URIRef) and str(obj).startswith(SPHN):
            records.setdefault(subject, str(obj)[len(SPHN):])
    return records


def _snake(iri: URIRef) -> str:
    local = str(iri).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    local = local[3:] if local.startswith("has") else local
    return "_".join(word.lower() for word in re.findall(r"[A-Z]?[a-z]+|[A-Z]+|\d+", local)) or "value"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _render_expression(node) -> str:
    """The expression as the model wrote it, as far as the algebra tells: ?a <= (?b <= 90), bif:datediff("day", ?a, ?b)."""
    if isinstance(node, Variable):
        return f"?{node}"
    if isinstance(node, URIRef):
        return _compact(node)
    if isinstance(node, Literal):
        return str(node) if isinstance(node.toPython(), (int, float, Decimal)) else f'"{node}"'
    if isinstance(node, (list, tuple)):
        return ", ".join(_render_expression(item) for item in node)
    if not isinstance(node, CompValue):
        return str(node)
    name = node.name
    if name == "RelationalExpression":
        return f"{_render_expression(node.expr)} {node.op} {_render_expression(node.other)}"
    if name in ARITHMETIC_NODES:
        operators = list(node.op) if isinstance(node.op, list) else [node.op]
        others = list(node.other) if isinstance(node.other, list) else [node.other]
        rest = "".join(f" {op} {_render_expression(other)}" for op, other in zip(operators, others))
        return _render_expression(node.expr) + rest
    if name == "ConditionalAndExpression":
        return " && ".join(_render_expression(item) for item in [node.expr] + list(node.other))
    if name == "ConditionalOrExpression":
        return " || ".join(_render_expression(item) for item in [node.expr] + list(node.other))
    if name == "UnaryNot":
        return f"!{_render_expression(node.expr)}"
    if name == "UnaryMinus":
        return f"-{_render_expression(node.expr)}"
    if name == "Function":
        return f"{_compact(node.iri)}({', '.join(_render_expression(item) for item in node.expr)})"
    if name.startswith("Builtin_"):
        arguments = [value for key, value in node.items() if not key.startswith("_")]
        return f"{name[len('Builtin_'):]}({', '.join(_render_expression(item) for item in arguments)})"
    if name.startswith("Aggregate_"):
        return f"{name[len('Aggregate_'):].upper()}({'DISTINCT ' if node.get('distinct') else ''}{_render_expression(node.vars)})"
    return "..."
