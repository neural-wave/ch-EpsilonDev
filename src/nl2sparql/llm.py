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
        # Greedy by default; the extra body may set the sampling (thinking mode must not decode greedily).
        body = {"temperature": 0, **self.extra_body, "model": model, "messages": messages, "max_tokens": max_tokens}
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

    async def sample(self, model: str, messages: list[dict], n: int, temperature: float,
                     max_tokens: int = 800) -> list[LLMReply]:
        """n answers to the same messages from one request at `temperature`, which wins over the extra body's
        (the OpenAI "n" field, which vLLM supports; a server that ignores it returns fewer answers). The server
        reports one usage per request, the prompt once and the completion of every answer: the first answer
        carries it and the others 0, so the answers' tokens add up to the request's."""
        body = {**self.extra_body, "model": model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens, "n": n}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport, verify=self.verify_tls) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)
            response.raise_for_status()
            payload = response.json()
            answers = [(choice["message"]["content"] or "", choice.get("finish_reason")) for choice in payload["choices"]]
            usage = payload.get("usage") or {}
            tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise LLMError(f"LLM call failed: {error}") from error
        if not answers:
            raise LLMError("LLM call failed: the answer has no choices.")
        return [LLMReply(text, tokens if position == 0 else 0, finish_reason)
                for position, (text, finish_reason) in enumerate(answers)]
