"""LLM query agent: writes the SPARQL from the request and the context."""
from __future__ import annotations

import re

from nl2sparql.agents.prompts import QUERY_PROMPTS, QueryPrompt
from nl2sparql.llm import LLMClient
from nl2sparql.models import Context

FENCE_RE = re.compile(r"```(?:sparql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sparql(text: str) -> str:
    match = FENCE_RE.search(text)
    return (match.group(1) if match else text).strip() + "\n"


class LLMQueryAgent:
    def __init__(self, client: LLMClient, model: str, max_tokens: int = 900, prompt: QueryPrompt = QUERY_PROMPTS["v1"]):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.prompt = prompt

    def messages(self, text: str, context: Context, feedback: str | None = None) -> list[dict]:
        return [
            {"role": "system", "content": self.prompt.system},
            {"role": "user", "content": self.prompt.render_user(text, context, feedback)},
        ]

    async def generate(self, text: str, context: Context, feedback: str | None = None) -> tuple[str, int]:
        reply = await self.client.chat(self.model, self.messages(text, context, feedback), max_tokens=self.max_tokens)
        return extract_sparql(reply.text), reply.tokens
