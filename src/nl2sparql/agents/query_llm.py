"""LLM query agent: writes the SPARQL from the request and the context."""
from __future__ import annotations

import re

from nl2sparql.agents.prompts import QUERY_SYSTEM, query_user_prompt
from nl2sparql.llm import LLMClient
from nl2sparql.models import Context

FENCE_RE = re.compile(r"```(?:sparql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sparql(text: str) -> str:
    match = FENCE_RE.search(text)
    return (match.group(1) if match else text).strip() + "\n"


class LLMQueryAgent:
    def __init__(self, client: LLMClient, model: str):
        self.client = client
        self.model = model

    async def generate(self, text: str, context: Context, feedback: str | None = None) -> tuple[str, int]:
        messages = [
            {"role": "system", "content": QUERY_SYSTEM},
            {"role": "user", "content": query_user_prompt(text, context, feedback)},
        ]
        reply = await self.client.chat(self.model, messages, max_tokens=900)
        return extract_sparql(reply.text), reply.tokens
