"""Agents console: a local page showing the full configuration of both agents and their prompts.

    GET  /agents          the page
    GET  /agents/config   configuration as JSON (secrets hidden)
    POST /agents/preview  {"text": ...} -> exactly what each model would receive (no model call)
    POST /agents/check    asks each configured LLM server which models it serves (one small request each)

Only /agents/check talks to the model server, and only when someone presses the button.
"""
from __future__ import annotations

from importlib import resources

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nl2sparql.agents.prompts import INTENT_PROMPTS, QUERY_PROMPTS
from nl2sparql.config import Settings
from nl2sparql.input_check import InputError, normalize_text
from nl2sparql.orchestrator import Pipeline

# Characters per token, measured with the Qwen3.5 tokenizer on our own prompts (2026-10-17):
# plain English instructions ~4, prompts full of SPARQL ~3.
CHARS_PER_TOKEN = {"intent": 4.0, "query": 3.0}
ENV_VARS = {
    "intent": ["LLM_INTENT_MODEL", "LLM_INTENT_BASE_URL", "LLM_INTENT_API_KEY", "LLM_INTENT_EXTRA_BODY",
               "LLM_INTENT_MAX_TOKENS", "INTENT_PROMPT_VERSION"],
    "query": ["LLM_QUERY_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_EXTRA_BODY", "LLM_QUERY_MAX_TOKENS",
              "QUERY_PROMPT_VERSION"],
}
ROLES = {
    "intent": "Reads only the request text and flags forbidden asks. Runs in parallel with the query agent.",
    "query": "Translates the request literally into SPARQL, using the context. Gets one retry with feedback.",
}
STAND_INS = {
    "intent": "Keyword rules (HeuristicIntentAgent): no model, 0 tokens.",
    "query": "Copies the SPARQL of the most similar example (ExampleQueryAgent): no model, 0 tokens.",
}


def estimate_tokens(text: str, agent: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN[agent]))


def hidden(secret: str) -> str:
    return "not set" if not secret or secret == "not-needed" else f"set ({len(secret)} characters, hidden)"


def build_console_router(settings: Settings, pipeline: Pipeline) -> APIRouter:
    router = APIRouter()

    def system_prompt(name: str) -> str:
        version = settings.agent(name).prompt_version
        return INTENT_PROMPTS[version] if name == "intent" else QUERY_PROMPTS[version].system

    def agent_config(name: str) -> dict:
        agent = pipeline.intent_agent if name == "intent" else pipeline.query_agent
        cfg = settings.agent(name)
        uses_llm = hasattr(agent, "client")
        return {
            "role": ROLES[name],
            "implementation": type(agent).__name__,
            "uses_llm": uses_llm,
            "stand_in": None if uses_llm else STAND_INS[name],
            "model": cfg.model or None,
            "server": cfg.base_url,
            "api_key": hidden(cfg.api_key),
            "verify_tls": cfg.verify_tls,
            "extra_body": cfg.extra_body,
            "max_answer_tokens": cfg.max_tokens,
            "temperature": 0,
            "json_mode": name == "intent",
            "timeout_seconds": cfg.timeout_seconds,
            "prompt_version": cfg.prompt_version,
            "prompt_versions_available": list(INTENT_PROMPTS if name == "intent" else QUERY_PROMPTS),
            "system_prompt": system_prompt(name),
            "env_vars": ENV_VARS[name],
        }

    @router.get("/agents", response_class=HTMLResponse)
    async def page():
        return resources.files("nl2sparql").joinpath("agents/console.html").read_text(encoding="utf-8")

    @router.get("/agents/config")
    async def config():
        builder = pipeline.context_builder
        return {
            "backend": settings.backend,
            "pipeline": {
                "max_retries": pipeline.max_retries,
                "max_input_chars": pipeline.max_input_chars,
                "return_noncompliant_query": settings.return_noncompliant_query,
                "property_kinds_loaded": len(pipeline.guard.kinds.kinds),
                "schema_classes_loaded": len(builder.schema.classes),
            },
            "agents": {"intent": agent_config("intent"), "query": agent_config("query")},
            "context": {
                "examples_per_request": builder.k_examples,
                "schema_cards_per_request": builder.k_cards,
                "code_candidates_per_request": builder.k_codes,
                "hide_exact_example": builder.exclude_exact_examples,
                "examples_in_bank": len(builder.examples.examples),
                "terminology_entries": len(builder.terminology.entries),
            },
        }

    @router.post("/agents/preview")
    async def preview(request: Request):
        try:
            body = await request.json()
            text = normalize_text(body.get("text") if isinstance(body, dict) else None, settings.max_input_chars)
        except (ValueError, InputError) as error:
            return JSONResponse({"error": str(error) or "The body must be JSON with a 'text' field."}, status_code=400)
        context = pipeline.context_builder.build(text)
        query_prompt = QUERY_PROMPTS[settings.agent("query").prompt_version]
        prompts = {
            "intent": [{"role": "system", "content": system_prompt("intent")}, {"role": "user", "content": text}],
            "query": [{"role": "system", "content": query_prompt.system},
                      {"role": "user", "content": query_prompt.render_user(text, context, None)}],
        }
        return {
            name: {
                "messages": messages,
                "estimated_prompt_tokens": estimate_tokens("".join(m["content"] for m in messages), name),
                "max_answer_tokens": settings.agent(name).max_tokens,
            }
            for name, messages in prompts.items()
        } | {
            "context": {
                "examples": [{"id": e.id, "question": e.question} for e in context.examples],
                "schema_cards": context.schema_cards,
                "code_candidates": [{"code": c.code, "system": c.system, "labels": list(c.labels)} for c in context.code_candidates],
            }
        }

    @router.post("/agents/check")
    async def check():
        results = {}
        for name in ("intent", "query"):
            cfg = settings.agent(name)
            if settings.backend != "llm":
                results[name] = {"status": "stub backend: no model server"}
                continue
            try:
                async with httpx.AsyncClient(timeout=5, verify=cfg.verify_tls) as client:
                    response = await client.get(f"{cfg.base_url.rstrip('/')}/models",
                                                headers={"Authorization": f"Bearer {cfg.api_key}"})
                response.raise_for_status()
                models = [model["id"] for model in response.json().get("data", [])]
                results[name] = {"status": "reachable", "models": models, "serves_configured_model": cfg.model in models}
            except Exception as error:
                results[name] = {"status": "unreachable", "error": str(error)[:200]}
        return results

    return router
