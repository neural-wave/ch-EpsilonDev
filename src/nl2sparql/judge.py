"""Judge: technical checks on the generated query, in code (no LLM).

First the repairs that need no model: a standard prefix declared with another namespace, the
inference option on a variable class, a date plus a duration string, three syntax mistakes with
exactly one meaning (judge_checks.syntax.normalize) and a typed value-set entry. Then the checks:
syntax, with hints that name the real error when rdflib still rejects the text (rdflib's own
message points at the wrong token); SPHN terms against the data snapshot; the inference option on
code patterns; distinct patient counts; and the three families in judge_checks/: variable scope,
semantics and schema; given the question, also that the query restricts to the coded concepts the
question names (judge_checks/coverage.py). Every error becomes the retry feedback, so a check that fires on a correct
query costs the request its retry: each one was measured at zero hits on the reference queries.
The repairs are reported so the pipeline can log them.

Database-agnostic by design: the judge never contacts a database (confirmed by the jury).
"""
from __future__ import annotations

from collections import Counter
import re

from rdflib import URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.context.schema import SchemaCards
from nl2sparql.context.terminology import TerminologyIndex
from nl2sparql.judge_checks import schema as schema_checks, scope as scope_checks, semantics as semantic_checks
from nl2sparql.judge_checks.coverage import check_question_coverage
from nl2sparql.judge_checks.syntax import diagnose, normalize
from nl2sparql.models import JudgeResult
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import SPHN, add_missing_prefixes, fix_standard_prefixes
from nl2sparql.sparql.terms import collect_iris

CODE_CLASS_ROOTS = (
    "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/",
    "https://biomedit.ch/rdf/sphn-resource/chop/",
)
PREFIX_DECL_RE = re.compile(r"\bPREFIX\s+([\w-]*):\s*<([^>]+)>", re.IGNORECASE)
CODE_INFERENCE_URI = "https://biomedit.ch/rdf/sphn-resource/global"
# A single pseudo-identifier triple that nothing reads is harmless for the result: not worth a retry.
SEMANTIC_CHECKS = [check for check in semantic_checks.CHECKS if check is not semantic_checks.check_unused_pseudo_identifier]
MAX_ERRORS = 5  # the model gets the first ones; more hints than that read as noise
# Errors after which the query still runs (the numbers may be off) are severity 1; wrong terms, scopes and
# dates make Virtuoso fail or return nothing (2); a syntax error is rejected outright (3).
HARD_ERROR_RE = re.compile(
    r"does not occur in the data| has no sphn:|needs OPTION \(inference|Count patients with COUNT\(DISTINCT"
    r"|bound twice|already bound|nests an aggregate|aggregates \?\w+, which is an alias|needs a dateTime"
    r"|is a record, not a date|datediff")


def severity(result: JudgeResult) -> tuple[int, int]:
    """How bad a verdict is, as (worst error, number of errors). The query agent regenerates the whole query
    on a retry, and on the 17 October run a retry that was still not clean was worse than the first answer as
    often as it was better: the pipeline keeps the less bad of the two attempts (26 of 36 ran instead of 21)."""
    worst = 0
    for error in result.errors:
        level = 3 if error.startswith("Syntax error") else 2 if HARD_ERROR_RE.search(error) else 1
        worst = max(worst, level)
    return (worst, len(result.errors))


class Judge:
    def __init__(self, schema: SchemaCards, terminology: TerminologyIndex | None = None):
        self.schema = schema
        self.terminology = terminology  # the codes a question names, for judge_checks/coverage.py

    def check(self, sparql: str, question: str | None = None) -> JudgeResult:
        repairs: list[str] = []
        text = fix_standard_prefixes(sparql.strip() + "\n")
        for rewrite in semantic_checks.REWRITES:
            text, applied = rewrite(text)
            repairs += applied
        fixed = add_missing_prefixes(text)
        parsed = parse_sparql(fixed)
        if parsed.kind == "Invalid":
            fixed, applied = normalize(fixed)
            repairs += applied
            parsed = parse_sparql(fixed)
            if parsed.kind == "Invalid":
                hints = diagnose(fixed) or [parsed.error]
                return JudgeResult(fixed, [f"Syntax error: {hint}" for hint in hints], repairs)
        if parsed.query is None:
            return JudgeResult(fixed, repairs=repairs)  # updates: nothing to check here, the guard rejects them
        for rewrite in schema_checks.REWRITES:
            rewritten, applied = rewrite(fixed, self.schema)
            if applied:
                reparsed = parse_sparql(rewritten)
                if reparsed.kind != "Invalid" and reparsed.query is not None:
                    fixed, parsed = rewritten, reparsed
                    repairs += applied
        errors = []
        # A class listed only inside VALUES next to classes the data has is an alternative that matches
        # nothing, not a mistake (official-08 lists ten variant classes, five of which the data does not
        # have). A lone misspelt class is still reported, and a property in VALUES is used as a predicate.
        in_values = _values_iris(parsed.algebra)
        only_in_values = {iri for iri in in_values - collect_iris(_without_values(parsed.algebra))
                          if str(iri).startswith(SPHN) and str(iri)[len(SPHN):][:1].isupper()}
        if not any(str(iri).startswith(SPHN) and self.schema.in_data(str(iri)[len(SPHN):]) for iri in in_values):
            only_in_values = set()
        for iri in sorted(collect_iris(parsed.algebra) - only_in_values):
            term = str(iri)[len(SPHN):] if str(iri).startswith(SPHN) else None
            if term and not self.schema.in_data(term):
                errors.append(schema_checks.suggest_missing_term(term, self.schema))
        if _code_patterns_missing_inference(fixed, parsed.algebra):
            errors.append(
                "Every ICD-10-GM or CHOP code type pattern needs "
                'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global").'
            )
        if _has_non_distinct_graph_count(parsed.algebra):
            errors.append("Count patients with COUNT(DISTINCT ?g) to avoid counting duplicate graph matches.")
        for check in scope_checks.CHECKS:
            errors += check(parsed)
        for check in schema_checks.CHECKS:
            errors += check(parsed, fixed, self.schema)
        for check in SEMANTIC_CHECKS:
            errors += check(parsed, fixed)
        if question:  # the query must restrict to the coded concepts the question names
            errors = check_question_coverage(question, parsed, fixed, self.schema, self.terminology) + errors
        return JudgeResult(fixed, _first_unique(errors), repairs)


def _values_iris(algebra) -> set:
    """IRIs that appear in VALUES blocks anywhere in the query."""
    found = set()

    def visit(node) -> None:
        if isinstance(node, (list, tuple, set)):
            for item in node:
                visit(item)
            return
        if not isinstance(node, CompValue):
            return
        if node.name == "values":
            for row in node.res or []:
                found.update(value for value in row.values() if isinstance(value, URIRef))
            return
        for key, value in node.items():
            if not key.startswith("_"):
                visit(value)

    visit(algebra)
    return found


def _without_values(node):
    """The algebra with every VALUES block replaced by an empty one, to see what the rest uses."""
    if isinstance(node, list):
        return [_without_values(item) for item in node]
    if isinstance(node, tuple):
        return tuple(_without_values(item) for item in node)
    if not isinstance(node, CompValue):
        return node
    if node.name == "values":
        return CompValue("values", res=[])
    copy = CompValue(node.name)
    for key, value in node.items():
        copy[key] = value if key.startswith("_") else _without_values(value)
    return copy


def _first_unique(errors: list[str]) -> list[str]:
    unique: list[str] = []
    for error in errors:
        if error not in unique:
            unique.append(error)
    return unique[:MAX_ERRORS]


def _code_type_patterns(algebra) -> list[tuple]:
    """Find typed ICD-10-GM and CHOP code patterns throughout the query algebra."""
    found = []

    def visit(node) -> None:
        if isinstance(node, (list, tuple, set)):
            for item in node:
                visit(item)
            return
        if not isinstance(node, CompValue):
            return
        if node.name == "BGP":
            for triple in node.triples:
                subject, predicate, obj = triple
                if (
                    predicate == RDF.type
                    and isinstance(subject, Variable)
                    and isinstance(obj, URIRef)
                    and any(str(obj).startswith(root) for root in CODE_CLASS_ROOTS)
                ):
                    found.append(triple)
            return
        for key, value in node.items():
            if not key.startswith("_"):
                visit(value)

    visit(algebra)
    return found


def _code_patterns_missing_inference(text: str, algebra) -> bool:
    """Require a global inference option on each typed code triple in the source query."""
    patterns = _code_type_patterns(algebra)
    if not patterns:
        return False
    prefixes = {namespace: prefix for prefix, namespace in PREFIX_DECL_RE.findall(text)}
    required = Counter((str(subject), str(obj)) for subject, _, obj in patterns)
    for (subject, iri), count in required.items():
        lexical_terms = [f"<{iri}>"]
        lexical_terms.extend(
            f"{prefix}:{iri[len(namespace):]}"
            for namespace, prefix in prefixes.items()
            if iri.startswith(namespace)
        )
        subject_pattern = rf"{re.escape('?' + str(subject))}"
        type_pattern = r"(?:a|(?:rdf\s*:\s*type))"
        option_count = 0
        for lexical in lexical_terms:
            triple_option = re.compile(
                subject_pattern + r"\s+" + type_pattern + r"\s+" + re.escape(lexical)
                + r"\s+OPTION\s*\(\s*inference\s+['\"]"
                + re.escape(CODE_INFERENCE_URI) + r"['\"]\s*\)",
                re.IGNORECASE,
            )
            option_count = max(option_count, sum(1 for _ in triple_option.finditer(text)))
        if option_count < count:
            return True
    return False


def _has_non_distinct_graph_count(algebra) -> bool:
    """Find COUNT(?g) aggregates unless their complete input is distinct patient graphs."""
    found = False

    def visit(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                visit(item)
            return
        if not isinstance(node, CompValue):
            return
        if node.name == "AggregateJoin":
            group = node.p
            for aggregate in node.A:
                if (
                    aggregate.name == "Aggregate_Count"
                    and aggregate.vars == Variable("g")
                    and not aggregate.distinct
                    and not _is_distinct_graph_subquery(group.p)
                ):
                    found = True
                    return
        for key, value in node.items():
            if not key.startswith("_"):
                visit(value)

    visit(algebra)
    return found


def _is_distinct_graph_subquery(node) -> bool:
    """Recognize an aggregate whose entire input is SELECT DISTINCT ?g { ... }."""
    if not isinstance(node, CompValue):
        return False
    if node.name == "ToMultiSet":
        node = node.p
    if not isinstance(node, CompValue) or node.name != "Distinct":
        return False
    project = node.p
    return (
        isinstance(project, CompValue)
        and project.name == "Project"
        and Variable("g") in project.PV
    )
