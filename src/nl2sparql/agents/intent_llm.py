"""LLM intent agent: reads the request text and returns the five flags as JSON."""
from __future__ import annotations

import json
import re

from nl2sparql.agents.prompts import INTENT_SYSTEM
from nl2sparql.llm import LLMClient
from nl2sparql.models import FLAG_NAMES, IntentFlags

JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_flags(text: str) -> IntentFlags:
    match = JSON_OBJECT_RE.search(text)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    flags = {name: data.get(name) is True for name in FLAG_NAMES}
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    rephrase = data.get("suggested_rephrase") if isinstance(data.get("suggested_rephrase"), str) else None
    if not any(flags.values()):
        reason = rephrase = None
    return IntentFlags(**flags, reason=reason, suggested_rephrase=rephrase)


class LLMIntentAgent:
    def __init__(self, client: LLMClient, model: str):
        self.client = client
        self.model = model

    async def classify(self, text: str) -> tuple[IntentFlags, int]:
        messages = [{"role": "system", "content": INTENT_SYSTEM}, {"role": "user", "content": text}]
        reply = await self.client.chat(self.model, messages, max_tokens=200, json_mode=True)
        return parse_flags(reply.text), reply.tokens
