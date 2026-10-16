"""HTTP API. POST /nl-to-sparql follows API_specification.md exactly.

/demo/* routes serve the demo page only: they are not part of the judged API.
"""
from __future__ import annotations

from importlib import resources

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nl2sparql.config import Settings
from nl2sparql.factory import build_pipeline
from nl2sparql.input_check import InputError
from nl2sparql.orchestrator import Pipeline, PipelineError
from nl2sparql.virtuoso import VirtuosoClient


def create_app(pipeline: Pipeline | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    pipeline = pipeline or build_pipeline(settings)
    app = FastAPI(title="Natural Language to SPARQL API", version="1.0.0")

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
        return payload

    @app.get("/health")
    async def health():
        return {"status": "ok", "backend": settings.backend}

    @app.get("/", response_class=HTMLResponse)
    async def demo_page():
        return resources.files("nl2sparql").joinpath("demo/index.html").read_text(encoding="utf-8")

    @app.post("/demo/ask")
    async def demo_ask(request: Request):
        result, error = await run(request)
        if error is not None:
            return error
        answer = {
            "sparql_query": result.sparql_query,
            "tokens_consumed": result.tokens_consumed,
            "is_compliant": result.is_compliant,
            "compliance_reason": result.compliance_reason,
            "suggested_rephrase": result.suggested_rephrase,
            "timings_ms": result.timings_ms,
            "executed": False,
        }
        if result.is_compliant:  # only compliant queries ever reach the knowledge base
            try:
                rows = await VirtuosoClient(settings.virtuoso_endpoint).select(result.sparql_query)
                answer.update(executed=True, columns=rows.columns, rows=rows.rows[:200], execution_ms=round(rows.elapsed_ms, 1))
            except Exception as error:  # the demo still shows the translation
                answer["execution_error"] = str(error)[:300]
        return answer

    return app
