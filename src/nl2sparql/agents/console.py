"""Agents console: a local page showing the full configuration of both agents and their prompts.

    GET  /agents          the page
    GET  /agents/config   configuration as JSON (secrets hidden)
    POST /agents/preview  {"text": ...} -> exactly what each model would receive (no model call)
    POST /agents/check    asks each configured LLM server which models it serves (one small request each)
    POST /agents/run      {"text": ...} -> runs the whole pipeline and returns what each agent did
    GET  /agents/reports  summaries of the saved eval reports (eval/reports/), newest first, with their notes
    GET  /agents/reports/<file>  one full report, with a row per question
    GET  /agents/runs     the runs page: metrics over runs as charts, and a run-against-run comparison
    POST /agents/runs/note  {"name": <report file>, "note": ...} -> what a run tested (eval/reports/notes.json)

Only /agents/check and /agents/run talk to the model server, and only when someone presses the button.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from importlib import resources
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nl2sparql.agents.prompts import INTENT_PROMPTS, QUERY_PROMPTS
from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.input_check import InputError, normalize_text
from nl2sparql.orchestrator import Pipeline, PipelineError

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
    "query": "Translates the request literally into SPARQL, using the context. Retried with feedback once by the "
             "judge (twice when the first retry was spent on syntax errors alone) and once by the guard.",
}
STAND_INS = {
    "intent": "Keyword rules (HeuristicIntentAgent): no model, 0 tokens.",
    "query": "Copies the SPARQL of the most similar example (ExampleQueryAgent): no model, 0 tokens.",
}


# eval/run_eval.py writes pipeline-<date>-<time>.json, eval/run_intent_eval.py intent-…, eval/run_query_eval.py
# query-…, eval/time_on_perf.py perf-…. Pipeline reports from before 2026-10-17 have no prefix.
REPORT_NAME_RE = re.compile(r"^(?:(pipeline|intent|query|perf)-)?(\d{8})-(\d{6})\.json$")


def read_report(path: Path) -> dict | None:
    """The whole report file, or None when it is not a readable eval report."""
    if not REPORT_NAME_RE.match(path.name) or not path.is_file():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) and isinstance(report.get("summary"), dict) else None


def report_entry(path: Path, notes: dict | None = None) -> dict | None:
    """Name, kind, date, note and summary of one report (the rows stay on disk until asked for)."""
    report = read_report(path)
    if report is None:
        return None
    kind, day, clock = REPORT_NAME_RE.match(path.name).groups()
    return {
        "name": path.name,
        "kind": kind or "pipeline",
        "created": f"{day[:4]}-{day[4:6]}-{day[6:]} {clock[:2]}:{clock[2:4]}",
        "note": (notes or {}).get(path.name, ""),
        "summary": report["summary"],
    }


def read_notes(folder: Path) -> dict:
    """What each run tested, written by eval/run_all.py or from the runs page."""
    path = folder / "notes.json"
    try:
        notes = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return notes if isinstance(notes, dict) else {}


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
                # CONTEXT_DEV_REFERENCES: the dev reference queries loaded as examples (0 with the flag off)
                "dev_references_in_bank": sum(e.source == "dev reference" for e in builder.examples.examples),
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

    @router.post("/agents/run")
    async def run(request: Request):
        """The whole pipeline on one question. With the LLM backend this calls the model server."""
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "The body must be JSON with a 'text' field."}, status_code=400)
        try:
            result = await pipeline.run(body.get("text") if isinstance(body, dict) else None)
        except InputError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except PipelineError as error:
            return JSONResponse({"error": str(error)}, status_code=503)
        # A developer page: it shows the query of forbidden requests too, whatever
        # RETURN_NONCOMPLIANT_QUERY says, because that query is what the query agent produced.
        return {"backend": settings.backend, **asdict(result), "flags_raised": result.intent.raised()}

    @router.get("/agents/runs", response_class=HTMLResponse)
    async def runs_page():
        return resources.files("nl2sparql").joinpath("agents/runs.html").read_text(encoding="utf-8")

    @router.post("/agents/runs/note")
    async def note(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "The body must be JSON with 'name' and 'note'."}, status_code=400)
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not REPORT_NAME_RE.match(name) or not (settings.reports_dir / name).is_file():
            return JSONResponse({"error": f"No eval report named {name!r}."}, status_code=404)
        notes = read_notes(settings.reports_dir)
        notes[name] = str(body.get("note") or "")[:300]
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        (settings.reports_dir / "notes.json").write_text(json.dumps(notes, indent=1, ensure_ascii=False), encoding="utf-8")
        return {"name": name, "note": notes[name]}

    @router.get("/agents/reports")
    async def reports():
        folder = settings.reports_dir
        notes = read_notes(folder) if folder.is_dir() else {}
        entries = [report_entry(path, notes) for path in folder.glob("*.json")] if folder.is_dir() else []
        entries = sorted((entry for entry in entries if entry),
                         key=lambda entry: REPORT_NAME_RE.match(entry["name"]).group(2, 3), reverse=True)
        try:
            shown = str(folder.relative_to(REPO_ROOT))
        except ValueError:
            shown = str(folder)
        return {"folder": shown, "reports": entries}

    @router.get("/agents/reports/{name}")
    async def report(name: str):
        found = read_report(settings.reports_dir / name) if REPORT_NAME_RE.match(name) else None
        if found is None:
            return JSONResponse({"error": f"No eval report named {name!r}."}, status_code=404)
        return found

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
