"""Interfaces of the two agents. Any backend (baseline, LLM, ...) only has to match these."""
from __future__ import annotations

from typing import Protocol

from nl2sparql.models import Context, IntentFlags


class IntentAgent(Protocol):
    async def classify(self, text: str) -> tuple[IntentFlags, int]:
        """Read the request text only. Returns (flags, tokens used)."""


class QueryAgent(Protocol):
    async def generate(self, text: str, context: Context, feedback: str | None = None) -> tuple[str, int]:
        """Translate the request literally into SPARQL. `feedback` explains what was wrong with the
        previous attempt (judge errors or a broken privacy rule). Returns (sparql, tokens used)."""
