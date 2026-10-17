"""The pipeline: input check, then intent agent in parallel with context -> query agent -> judge,
then the compliance guard. One retry per request, shared by the judge and the guard."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict
from pathlib import Path

from nl2sparql.agents.base import IntentAgent, QueryAgent
from nl2sparql.context.builder import ContextBuilder
from nl2sparql.guard.decision import ComplianceGuard
from nl2sparql.input_check import normalize_text
from nl2sparql.judge import Judge
from nl2sparql.models import Context, IntentFlags, PipelineResult

logger = logging.getLogger(__name__)

# Added to the retry feedback when the query agent's answer hit its max_tokens limit mid-query.
CUT_OFF_FEEDBACK = "Your previous answer was cut off at the length limit. Write a shorter query and no explanation."


class PipelineError(RuntimeError):
    """The pipeline could not produce a query (e.g. the model server is down). The API answers 503."""


class _Run:
    """Bookkeeping of one request: time and tokens per step, why a retry happened."""

    def __init__(self, max_retries: int):
        self.retries_left = max_retries
        self.timings: dict[str, float] = {}
        self.tokens: dict[str, int] = {}
        self.diagnostics: dict = {}

    def take_retry(self, reason: str) -> bool:
        if self.retries_left <= 0:
            return False
        self.retries_left -= 1
        self.diagnostics["retry_reason"] = reason
        return True

    def add_tokens(self, step: str, used: int) -> None:
        self.tokens[step] = self.tokens.get(step, 0) + used

    def note_cut_off(self, step: str) -> None:
        self.diagnostics.setdefault("cut_off", []).append(step)


class Pipeline:
    def __init__(self, intent_agent: IntentAgent, query_agent: QueryAgent, context_builder: ContextBuilder,
                 judge: Judge, guard: ComplianceGuard, max_input_chars: int = 2000, max_retries: int = 1,
                 log_path: Path | None = None, request_timeout_seconds: float = 75.0):
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be a positive finite number.")
        self.intent_agent = intent_agent
        self.query_agent = query_agent
        self.context_builder = context_builder
        self.judge = judge
        self.guard = guard
        self.max_input_chars = max_input_chars
        self.max_retries = max_retries
        self.log_path = log_path
        self.request_timeout_seconds = request_timeout_seconds

    async def run(self, raw_text: str) -> PipelineResult:
        deadline = asyncio.get_running_loop().time() + self.request_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                result = await self._run(raw_text)
                # Check any synchronous work between the final await and the response.
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                return result
        except TimeoutError as error:
            raise PipelineError(f"Request timed out after {self.request_timeout_seconds:g} seconds.") from error

    async def _run(self, raw_text: str) -> PipelineResult:
        started = time.perf_counter()
        text = normalize_text(raw_text, self.max_input_chars)
        run = _Run(self.max_retries)

        intent_task = asyncio.create_task(self._classify(text, run))
        stage = "Context building"
        try:
            context = await asyncio.to_thread(self._timed, run, "context", lambda: self.context_builder.build(text))
            stage = "Query generation"
            sparql = await self._generate(text, context, None, run, intent_task)
            flags = await intent_task
        except BaseException as error:
            # Do not leave the concurrent intent call running after a failed or cancelled request.
            intent_task.cancel()
            await asyncio.gather(intent_task, return_exceptions=True)
            if not isinstance(error, Exception) or isinstance(error, PipelineError):
                raise
            raise PipelineError(f"{stage} failed: {error}") from error

        try:
            decision = await asyncio.to_thread(self._timed, run, "guard", lambda: self.guard.decide(sparql, flags))
        except Exception as error:
            raise PipelineError("Compliance check failed.") from error
        if decision.retry_hint and run.take_retry("guard"):
            run.diagnostics["guard_violations_first_try"] = [f"{v.rule}: {v.message}" for v in decision.violations]
            run.diagnostics["first_try_sparql"] = sparql
            try:
                candidate_sparql = await self._generate(text, context, decision.retry_hint, run, intent_task)
                candidate_decision = await asyncio.to_thread(
                    self._timed, run, "guard_retry", lambda: self.guard.decide(candidate_sparql, flags)
                )
            except Exception as error:  # keep the first answer if the retry fails
                logger.warning("retry after guard failed: %s", error)
            else:
                sparql, decision = candidate_sparql, candidate_decision

        run.timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        result = PipelineResult(
            sparql_query=sparql,
            tokens_consumed=sum(run.tokens.values()),
            is_compliant=decision.compliant,
            compliance_reason=decision.reason,
            suggested_rephrase=decision.suggested_rephrase,
            intent=flags,
            violations=[f"{v.rule}: {v.message}" for v in decision.violations],
            retries=self.max_retries - run.retries_left,
            timings_ms=run.timings,
            tokens_by_step=run.tokens,
            diagnostics=run.diagnostics,
        )
        if self.log_path is not None:
            await asyncio.to_thread(self._log, text, result)
        return result

    async def _classify(self, text: str, run: _Run) -> IntentFlags:
        started = time.perf_counter()
        try:
            detailed = getattr(self.intent_agent, "classify_detailed", None)  # LLM agents report more
            if detailed is None:
                flags, used = await self.intent_agent.classify(text)
            else:
                reply = await detailed(text)
                flags, used = reply.flags, reply.tokens
                if not reply.valid_json:
                    run.diagnostics["intent_invalid_json"] = True
                if reply.truncated:
                    run.note_cut_off("intent")
            run.add_tokens("intent", used)
            return flags
        except Exception as error:  # the verdict needs the intent flags: the request fails (HTTP 503)
            raise PipelineError("Intent classification failed.") from error
        finally:
            run.timings["intent"] = round((time.perf_counter() - started) * 1000, 1)

    async def _generate(self, text: str, context: Context, feedback: str | None, run: _Run,
                        intent_task: asyncio.Task[IntentFlags]) -> str:
        """Query agent + judge. The first call counts as "query", any later one as "query_retry"."""
        started = time.perf_counter()
        step = "query" if feedback is None else "query_retry"
        sparql, cut_off = await self._ask_query_agent(text, context, feedback, step, run)
        verdict = await asyncio.to_thread(self.judge.check, sparql)
        # The first generation runs alongside the intent agent, but a forbidden request is never retried.
        if verdict.errors and run.retries_left > 0 and not (await intent_task).flagged and run.take_retry("judge"):
            run.diagnostics["judge_errors_first_try"] = verdict.errors
            run.diagnostics["first_try_sparql"] = verdict.sparql
            retry_feedback = " ".join(([CUT_OFF_FEEDBACK] if cut_off else []) + verdict.errors)
            sparql, _ = await self._ask_query_agent(text, context, retry_feedback, "query_retry", run)
            verdict = await asyncio.to_thread(self.judge.check, sparql)
        if verdict.errors:
            run.diagnostics["judge_errors_final"] = verdict.errors
        run.timings["generate" if feedback is None else "generate_retry"] = round((time.perf_counter() - started) * 1000, 1)
        return verdict.sparql

    async def _ask_query_agent(self, text: str, context: Context, feedback: str | None, step: str,
                               run: _Run) -> tuple[str, bool]:
        """One query agent call. Returns (sparql, whether the answer was cut off at max_tokens)."""
        detailed = getattr(self.query_agent, "generate_detailed", None)  # LLM agents report more
        if detailed is None:
            sparql, used = await self.query_agent.generate(text, context, feedback)
            run.add_tokens(step, used)
            return sparql, False
        reply = await detailed(text, context, feedback)
        run.add_tokens(step, reply.tokens)
        if reply.truncated:
            run.note_cut_off(step)
        return reply.sparql, reply.truncated

    @staticmethod
    def _timed(run: _Run, key: str, function):
        started = time.perf_counter()
        try:
            return function()
        finally:
            run.timings[key] = round((time.perf_counter() - started) * 1000, 1)

    def _log(self, text: str, result: PipelineResult) -> None:
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {"text": text, **asdict(result)}
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:
            logger.warning("could not write request log: %s", error)
