"""Baseline query agent: returns the SPARQL of the most similar example. No model, 0 tokens.

It is not a translator; it only lets the pipeline run end to end before the LLM is connected.
"""
from __future__ import annotations

from nl2sparql.models import Context

FALLBACK_QUERY = """PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
SELECT (COUNT(DISTINCT ?g) AS ?patients)
WHERE { GRAPH ?g { ?case a sphn:AdministrativeCase } }
"""


class ExampleQueryAgent:
    async def generate(self, text: str, context: Context, feedback: str | None = None) -> tuple[str, int]:
        if context.examples:
            return context.examples[0].sparql, 0
        return FALLBACK_QUERY, 0
