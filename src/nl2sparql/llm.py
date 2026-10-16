"""OpenAI-compatible chat client (vLLM on the hackathon GPU, Ollama locally, ...)."""
from __future__ import annotations

from dataclasses import dataclass

import httpx


class LLMError(RuntimeError):
    """The model server failed or returned something unusable."""


@dataclass(frozen=True)
class LLMReply:
    text: str
    tokens: int  # prompt + completion tokens reported by the server
    finish_reason: str | None = None  # "length" means the answer hit max_tokens and was cut off


class LLMClient:
    def __init__(self, base_url: str, api_key: str = "not-needed", timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None, verify_tls: bool = True,
                 extra_body: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout = timeout
        self.transport = transport
        self.verify_tls = verify_tls  # False for servers with a self-signed certificate (Vast.ai templates)
        # Extra request fields, e.g. {"chat_template_kwargs": {"enable_thinking": false}} for Qwen3.x:
        # with thinking on, the model can spend its whole token budget reasoning and return nothing.
        self.extra_body = extra_body or {}

    async def chat(self, model: str, messages: list[dict], max_tokens: int = 800, json_mode: bool = False) -> LLMReply:
        body = {**self.extra_body, "model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport, verify=self.verify_tls) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            text = choice["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
            raise LLMError(f"LLM call failed: {error}") from error
        usage = payload.get("usage") or {}
        tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        return LLMReply(text, tokens, choice.get("finish_reason"))
