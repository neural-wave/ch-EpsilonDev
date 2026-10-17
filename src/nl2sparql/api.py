"""HTTP API. POST /nl-to-sparql follows API_specification.md exactly.

The pipeline is database-agnostic: nothing behind /nl-to-sparql connects to a database.
/demo/* routes serve the demo page only: they are not part of the judged API. The team's console
(frontend/, module E) is served at /console/ from the same origin, so it needs no proxy and no CORS.
"""
from __future__ import annotations

from dataclasses import asdict
from importlib import resources
from datetime import datetime, timezone
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from nl2sparql.agents.console import build_console_router
from nl2sparql.assist import clarify, follow_ups
from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.demo_shares import ShareError, ShareStore
from nl2sparql.demo_results import POLICY_VERSION, ROW_LIMIT, process_results
from nl2sparql.factory import build_pipeline
from nl2sparql.input_check import InputError, normalize_text
from nl2sparql.orchestrator import Pipeline, PipelineError
from nl2sparql.virtuoso import VirtuosoClient


def create_app(pipeline: Pipeline | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.demo_public_mode and (type(settings.demo_min_group_size) is not int or settings.demo_min_group_size < 1):
        raise ValueError("DEMO_PUBLIC_MODE requires an approved positive DEMO_MIN_GROUP_SIZE.")
    pipeline = pipeline or build_pipeline(settings)
    if settings.demo_public_mode:
        pipeline.log_path = None  # End-user questions and generated queries must not enter request archives.
    shares = ShareStore(settings.demo_share_db_path, settings.demo_share_ttl_seconds)
    app = FastAPI(title="Natural Language to SPARQL API", version="1.0.0")
    app.include_router(build_console_router(settings, pipeline))  # /agents: local agents console

    async def run(request: Request, *, public_errors: bool = False):
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
            return None, JSONResponse({"error": "The request could not be processed." if public_errors else str(error)}, status_code=503)

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

    @app.middleware("http")
    async def private_demo_responses(request: Request, call_next):
        path = request.url.path
        if settings.demo_public_mode and not (path in {"/", "/health"} or path.startswith("/demo/") or path.startswith("/console/")):
            return JSONResponse({"error": "Not found."}, status_code=404, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        if path == "/" or path.startswith(("/demo/", "/console/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.post("/demo/shares")
    async def create_share(request: Request):
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 2048:
                    return JSONResponse({"error": "Expected a share_token."}, status_code=400)
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {"share_token"} or not isinstance(body["share_token"], str):
                return JSONResponse({"error": "Expected a share_token."}, status_code=400)
            return await asyncio.to_thread(shares.publish, body["share_token"], settings.demo_min_group_size)
        except ValueError:
            return JSONResponse({"error": "Expected a share_token."}, status_code=400)
        except ShareError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        except Exception:
            return JSONResponse({"error": "Sharing is temporarily unavailable."}, status_code=503)

    @app.get("/demo/shares/{share_id}")
    async def read_share(share_id: str):
        try:
            return await asyncio.to_thread(shares.get, share_id, settings.demo_min_group_size)
        except ShareError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        except Exception:
            return JSONResponse({"error": "Sharing is temporarily unavailable."}, status_code=503)

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

    @app.get("/demo/config")
    async def demo_config():
        try:
            threshold = settings.demo_min_group_size
            if threshold is not None and (type(threshold) is not int or threshold < 1):
                raise ValueError("Invalid demo threshold")
            if type(ROW_LIMIT) is not int or ROW_LIMIT != 200 or type(POLICY_VERSION) is not int or POLICY_VERSION != 1:
                raise ValueError("Invalid demo policy metadata")
            return {"demo_min_group_size": threshold, "row_limit": ROW_LIMIT, "policy_version": POLICY_VERSION}
        except Exception:
            return JSONResponse({"error": "Demo privacy configuration unavailable."}, status_code=503)

    @app.post("/demo/ask")
    async def demo_ask(request: Request):
        # The team console shows the details; DEMO_SHOW_DETAILS=false or the public mode hides them.
        details = settings.demo_show_details and not settings.demo_public_mode
        result, error = await run(request, public_errors=not details)
        if error is not None:
            return error
        not_a_question = bool(result.diagnostics.get("not_a_question"))  # "hey", "thanks": no query at all
        answer = {
            # The demo never shows the query of a forbidden request. Without details it shows no query at all:
            # the executed query stays internal to the demo, and the judged endpoint is unchanged either way.
            "sparql_query": result.sparql_query if details and result.is_compliant else None,
            "tokens_consumed": result.tokens_consumed,
            "is_compliant": result.is_compliant,
            "not_a_question": not_a_question,
            "compliance_reason": result.compliance_reason if details or not_a_question else (
                None if result.is_compliant else "This request cannot be answered. Only permitted aggregate results may be shown."),
            "suggested_rephrase": result.suggested_rephrase if details else None,
            "timings_ms": result.timings_ms,
            "tokens_by_step": result.tokens_by_step,
            "diagnostics": result.diagnostics if details else {},  # retry diagnostics may contain generated query text
            "executed": False,
            "follow_ups": [],
            "result_status": "not_executed",
            "suppression": None,
            "column_metadata": [],
            "reproducibility": None,
        }
        # Optional dev check, outside the system: run a compliant query on the local Virtuoso to see
        # the result and its execution time. The pipeline itself never touches a database.
        if result.is_compliant:
            try:
                executed_at = datetime.now(timezone.utc).isoformat()
                rows = await VirtuosoClient(settings.virtuoso_endpoint).select(result.sparql_query)
                processed = process_results(result.sparql_query, rows.columns, rows.rows, settings.demo_min_group_size)
                answer.update(executed=True, execution_ms=round(rows.elapsed_ms, 1), **processed)
                if processed["result_status"] == "released":
                    body = await request.json()
                    answer["reproducibility"] = {
                        "source": "database",
                        "question": body["text"],
                        "executed_at": executed_at,
                        "dataset": {
                            "name": settings.demo_dataset_name,
                            "version": settings.demo_dataset_version,
                            "description": settings.demo_dataset_description,
                        },
                    }
                    try:
                        question = normalize_text(body["text"], settings.max_input_chars)
                        token = shares.stage(question, processed)
                        if token:
                            answer["share_token"] = token
                    except Exception:
                        answer["sharing_error"] = "Sharing is temporarily unavailable."
            except Exception as error:  # without details, no database or query detail reaches the user
                answer["result_status"] = "execution_error"
                answer["execution_error"] = str(error)[:300] if details else "The database result could not be retrieved."
        if answer.get("result_status") == "released" and answer.get("rows"):
            # Ways to break the answer down, when it is one number of at least 2; never for a withheld result.
            text = (await request.json())["text"]  # the run above validated the body
            found = follow_ups(text, answer["rows"], pipeline.context_builder.schema)
            answer["follow_ups"] = [asdict(follow_up) for follow_up in found]
        return answer

    return app
