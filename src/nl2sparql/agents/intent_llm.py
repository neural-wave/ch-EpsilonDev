"""LLM intent agent: reads the request text and returns the five flags as JSON."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from nl2sparql.agents.prompts import INTENT_SYSTEM
from nl2sparql.llm import LLMClient
from nl2sparql.models import FLAG_NAMES, IntentFlags

JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class IntentReply:
    """One classification with the details the intent eval needs."""

    flags: IntentFlags
    tokens: int
    raw: str
    valid_json: bool


def parse_flags_checked(text: str) -> tuple[IntentFlags, bool]:
    """Flags from the model's answer, plus whether the answer was a usable JSON object."""
    match = JSON_OBJECT_RE.search(text)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    valid = isinstance(data, dict) and all(isinstance(data.get(name), bool) for name in FLAG_NAMES)
    data = data if isinstance(data, dict) else {}
    flags = {name: data.get(name) is True for name in FLAG_NAMES}
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    rephrase = data.get("suggested_rephrase") if isinstance(data.get("suggested_rephrase"), str) else None
    if not any(flags.values()):
        reason = rephrase = None
    return IntentFlags(**flags, reason=reason, suggested_rephrase=rephrase), valid


def parse_flags(text: str) -> IntentFlags:
    return parse_flags_checked(text)[0]


class LLMIntentAgent:
    def __init__(self, client: LLMClient, model: str, max_tokens: int = 200, system_prompt: str = INTENT_SYSTEM):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt

    def messages(self, text: str) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": text}]

    async def classify_detailed(self, text: str) -> IntentReply:
        reply = await self.client.chat(self.model, self.messages(text), max_tokens=self.max_tokens, json_mode=True)
        flags, valid = parse_flags_checked(reply.text)
        return IntentReply(flags, reply.tokens, reply.text, valid)

    async def classify(self, text: str) -> tuple[IntentFlags, int]:
        result = await self.classify_detailed(text)
        return result.flags, result.tokens
