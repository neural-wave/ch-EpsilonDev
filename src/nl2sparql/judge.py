"""Judge: technical checks on the generated query, in code (no LLM).

Adds missing standard prefixes, checks syntax and SPHN terms against the data snapshot, and
reports known query-style issues for code inference and non-distinct patient graph counts.
Any error triggers the shared retry with the error text as feedback.

Database-agnostic by design: the judge never contacts a database (confirmed by the jury).
"""
from __future__ import annotations

from collections import Counter
import re

from rdflib import URIRef, Variable
from rdflib.namespace import RDF
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.context.schema import SchemaCards
from nl2sparql.models import JudgeResult
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import SPHN, add_missing_prefixes
from nl2sparql.sparql.terms import collect_iris

CODE_CLASS_ROOTS = (
    "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/",
    "https://biomedit.ch/rdf/sphn-resource/chop/",
)
PREFIX_DECL_RE = re.compile(r"\bPREFIX\s+([\w-]*):\s*<([^>]+)>", re.IGNORECASE)
CODE_INFERENCE_URI = "https://biomedit.ch/rdf/sphn-resource/global"


class Judge:
    def __init__(self, schema: SchemaCards):
        self.schema = schema

    def check(self, sparql: str) -> JudgeResult:
        fixed = add_missing_prefixes(sparql.strip() + "\n")
        parsed = parse_sparql(fixed)
        if parsed.kind == "Invalid":
            return JudgeResult(fixed, [f"Syntax error: {parsed.error}"])
        if parsed.query is None:
            return JudgeResult(fixed)  # updates: nothing to check here, the guard rejects them
        errors = _style_errors(fixed)
        for iri in sorted(collect_iris(parsed.algebra)):
            term = str(iri)[len(SPHN):] if str(iri).startswith(SPHN) else None
            if term and not self.schema.in_data(term):
                suggestion = self.schema.closest(term)
                hint = f" Did you mean sphn:{suggestion}?" if suggestion else ""
                errors.append(f"sphn:{term} does not occur in the data.{hint}")
        if _code_patterns_missing_inference(fixed, parsed.algebra):
            errors.append(
                "Every ICD-10-GM or CHOP code type pattern needs "
                'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global").'
            )
        if _has_non_distinct_graph_count(parsed.algebra):
            errors.append("Count patients with COUNT(DISTINCT ?g) to avoid counting duplicate graph matches.")
        return JudgeResult(fixed, errors)


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
