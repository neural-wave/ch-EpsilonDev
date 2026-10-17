"""HTTP API. POST /nl-to-sparql follows API_specification.md exactly.

The pipeline is database-agnostic: nothing behind /nl-to-sparql connects to a database.
/demo/* routes serve the demo page only: they are not part of the judged API. The team's console
(frontend/, module E) is served at /console/ from the same origin, so it needs no proxy and no CORS.
"""
from __future__ import annotations

from dataclasses import asdict
from importlib import resources

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from nl2sparql.agents.console import build_console_router
from nl2sparql.assist import clarify, follow_ups
from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.input_check import InputError, normalize_text
from nl2sparql.orchestrator import Pipeline, PipelineError
from nl2sparql.virtuoso import VirtuosoClient


def create_app(pipeline: Pipeline | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    pipeline = pipeline or build_pipeline(settings)
    app = FastAPI(title="Natural Language to SPARQL API", version="1.0.0")
    app.include_router(build_console_router(settings, pipeline))  # /agents: local agents console

    async def run(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return None, JSONResponse({"error": "The body must be JSON with a 'text' field."}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        try:
            return await pipeline.run(text), None
        except InputError as error:
            return None, JSONResponse({"error": str(error)}, status_code=400)
        except PipelineError as error:
            return None, JSONResponse({"error": str(error)}, status_code=503)

    @app.post("/nl-to-sparql")
    async def nl_to_sparql(request: Request):
        result, error = await run(request)
        if error is not None:
            return error
        payload = {
            "sparql_query": result.sparql_query,
            "tokens_consumed": result.tokens_consumed,
            "is_compliant": result.is_compliant,
        }
        if not result.is_compliant:
            payload["compliance_reason"] = result.compliance_reason
            if not settings.return_noncompliant_query:  # real use: the user gets only the reason
                payload["sparql_query"] = ""
        return payload

    @app.get("/health")
    async def health():
        return {"status": "ok", "backend": settings.backend}

    console = REPO_ROOT / "frontend"
    if console.is_dir():  # the console's pages reference ../js and ../css, so the whole folder is mounted
        app.mount("/console", StaticFiles(directory=str(console), html=True), name="console")

    @app.get("/", response_class=HTMLResponse)
    async def demo_page():
        if console.is_dir():
            return RedirectResponse("/console/cli/")
        return resources.files("nl2sparql").joinpath("demo/index.html").read_text(encoding="utf-8")

    @app.post("/demo/clarify")
    async def demo_clarify(request: Request):
        """"Did you mean...?" choices for a vague question, before it is asked. Code only: no model call."""
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "The body must be JSON with a 'text' field."}, status_code=400)
        try:
            text = normalize_text(body.get("text") if isinstance(body, dict) else None, settings.max_input_chars)
        except InputError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        found = clarify(text, pipeline.context_builder.terminology)
        return found.to_json() if found else {"prompt": None, "choices": []}

    @app.post("/demo/ask")
    async def demo_ask(request: Request):
        result, error = await run(request)
        if error is not None:
            return error
        answer = {
            # The demo shows what a user would see: never the query of a forbidden request.
            "sparql_query": result.sparql_query if result.is_compliant else None,
            "tokens_consumed": result.tokens_consumed,
            "is_compliant": result.is_compliant,
            "compliance_reason": result.compliance_reason,
            "suggested_rephrase": result.suggested_rephrase,
            "timings_ms": result.timings_ms,
            "tokens_by_step": result.tokens_by_step,
            "diagnostics": result.diagnostics,
            "executed": False,
            "follow_ups": [],
        }
        # Optional dev check, outside the system: run a compliant query on the local Virtuoso to see
        # the result and its execution time. The pipeline itself never touches a database.
        if result.is_compliant:
            try:
                rows = await VirtuosoClient(settings.virtuoso_endpoint).select(result.sparql_query)
                answer.update(executed=True, columns=rows.columns, rows=rows.rows[:200], execution_ms=round(rows.elapsed_ms, 1))
            except Exception as error:  # the demo still shows the translation
                answer["execution_error"] = str(error)[:300]
        if answer["executed"]:  # ways to break the answer down, when it is one number of at least 2
            text = (await request.json())["text"]  # the run above validated the body
            found = follow_ups(text, answer["rows"], pipeline.context_builder.schema)
            answer["follow_ups"] = [asdict(follow_up) for follow_up in found]
        return answer

    return app
