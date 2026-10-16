"""Judge: technical checks on the generated query, in code (no LLM).

Base version: add missing standard prefixes, check the syntax, and flag SPHN classes or
properties that never occur in the data (e.g. sphn:hasRankCode from the 2026.1 schema).
Any error triggers the shared retry with the error text as feedback.
"""
from __future__ import annotations

from nl2sparql.context.schema import SchemaCards
from nl2sparql.models import JudgeResult
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import SPHN, add_missing_prefixes
from nl2sparql.sparql.terms import collect_iris


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
        errors = []
        for iri in sorted(collect_iris(parsed.algebra)):
            term = str(iri)[len(SPHN):] if str(iri).startswith(SPHN) else None
            if term and not self.schema.in_data(term):
                suggestion = self.schema.closest(term)
                hint = f" Did you mean sphn:{suggestion}?" if suggestion else ""
                errors.append(f"sphn:{term} does not occur in the data.{hint}")
        return JudgeResult(fixed, errors)
