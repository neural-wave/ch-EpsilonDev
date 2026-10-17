"""Judge: technical checks on the generated query, in code (no LLM).

Base version: add missing standard prefixes, check the syntax, and flag SPHN classes or
properties that never occur in the data (e.g. sphn:hasRankCode from the 2026.1 schema).
Any error triggers the shared retry with the error text as feedback.

Database-agnostic by design: the judge never contacts a database (confirmed by the jury).
"""
from __future__ import annotations

import re

from nl2sparql.context.schema import SchemaCards
from nl2sparql.models import JudgeResult
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import SPHN, add_missing_prefixes
from nl2sparql.sparql.terms import collect_iris

CODE_TYPE_RE = re.compile(
    r"(?:\ba|rdf:type)\s+(?:icd-10-gm|chop):[A-Za-z0-9._-]+"
    r"(?P<tail>\s*(?:OPTION\s*\(\s*inference\b[^)]*\)\s*)?[.;}])",
    re.IGNORECASE,
)
NON_DISTINCT_GRAPH_COUNT_RE = re.compile(r"COUNT\s*\(\s*(?!DISTINCT\b)\?g\s*\)", re.IGNORECASE)


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
        return JudgeResult(fixed, errors)


def _style_errors(sparql: str) -> list[str]:
    """Catch known query shapes that parse but are wrong for this dataset."""
    errors = []
    if any("OPTION" not in match.group("tail").upper() for match in CODE_TYPE_RE.finditer(sparql)):
        errors.append(
            "Code type patterns using icd-10-gm or chop must include OPTION (inference "
            '"https://biomedit.ch/rdf/sphn-resource/global").'
        )
    if NON_DISTINCT_GRAPH_COUNT_RE.search(sparql):
        errors.append("COUNT(?g) counts graph bindings, not patients; use COUNT(DISTINCT ?g).")
    return errors
