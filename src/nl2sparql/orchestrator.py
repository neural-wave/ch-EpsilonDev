"""The pipeline: input check, then intent agent in parallel with context -> query agent -> judge,
then the compliance guard. The judge and the guard each have their own retry budget (MAX_RETRIES per
checker), so with the default of 1 the query agent is called at most three times per request."""
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
from nl2sparql.intent_check import confirm_identifier_flag
from nl2sparql.judge import Judge, severity
from nl2sparql.models import Context, IntentFlags, PipelineResult

logger = logging.getLogger(__name__)

# Added to the retry feedback when the query agent's answer hit its max_tokens limit mid-query.
CUT_OFF_FEEDBACK = "Your previous answer was cut off at the length limit. Write a shorter query and no explanation."
PREVIOUS_QUERY_CHARS = 2500  # how much of the rejected query the retry feedback carries


class PipelineError(RuntimeError):
    """The pipeline could not produce a query (e.g. the model server is down). The API answers 503."""


class _Run:
    """Bookkeeping of one request: time and tokens per step, which checker asked for a retry."""

    def __init__(self, max_retries: int):
        # One budget per checker: a guard retry that comes back unparseable still gets the judge's retry,
        # and a judge retry that still breaks a rule still gets the guard's.
        self.retries_left = {"judge": max_retries, "guard": max_retries}
        self.retries = 0
        self.timings: dict[str, float] = {}
        self.tokens: dict[str, int] = {}
        self.diagnostics: dict = {}

    def can_retry(self, checker: str) -> bool:
        return self.retries_left[checker] > 0

    def take_retry(self, checker: str) -> bool:
        if not self.can_retry(checker):
            return False
        self.retries_left[checker] -= 1
        self.retries += 1
        taken = set(self.diagnostics.get("retry_reason", "").split("+")) | {checker}
        self.diagnostics["retry_reason"] = "+".join(name for name in ("judge", "guard") if name in taken)
        return True

    def note_rejected(self, key: str, problems: list[str], sparql: str) -> None:
        """What a checker rejected before its retry. `first_try_sparql` keeps the first answer; when the
        rejected query is a retry answer it gets its own key, so both retries can be reproduced."""
        self.diagnostics[key] = problems
        if self.diagnostics.setdefault("first_try_sparql", sparql) != sparql:
            self.diagnostics["rejected_retry_sparql"] = sparql

    def add_tokens(self, step: str, used: int) -> None:
        self.tokens[step] = self.tokens.get(step, 0) + used

    def note_repairs(self, repairs: list[str]) -> None:
        if repairs:  # what the judge fixed without the model
            self.diagnostics.setdefault("judge_repairs", []).extend(repairs)

    def note_cut_off(self, step: str) -> None:
        self.diagnostics.setdefault("cut_off", []).append(step)


class Pipeline:
    def __init__(self, intent_agent: IntentAgent, query_agent: QueryAgent, context_builder: ContextBuilder,
                 judge: Judge, guard: ComplianceGuard, max_input_chars: int = 2000, max_retries: int = 1,
                 log_path: Path | None = None, request_timeout_seconds: float = 75.0,
                 retry_with_previous_query: bool = False):
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
        self.retry_with_previous_query = retry_with_previous_query

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
            run.note_rejected("guard_violations_first_try", [f"{v.rule}: {v.message}" for v in decision.violations], sparql)
            try:
                candidate_sparql = await self._generate(text, context, self._feedback(decision.retry_hint, sparql), run, intent_task)
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
            retries=run.retries,
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
                if reply.valid_json is False:
                    run.diagnostics["intent_invalid_json"] = True
                if reply.truncated:
                    run.note_cut_off("intent")
                if reply.fallback:
                    run.diagnostics["intent_fallback"] = reply.fallback
            run.add_tokens("intent", used)
            confirmed = confirm_identifier_flag(text, flags)
            if confirmed != flags:
                run.diagnostics["intent_flag_cleared"] = "asks_for_identifiers"
            return confirmed
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
        verdict = await asyncio.to_thread(self.judge.check, sparql, text)
        run.note_repairs(verdict.repairs)
        # The first generation runs alongside the intent agent, but a forbidden request is never retried.
        if verdict.errors and run.can_retry("judge") and not (await intent_task).flagged and run.take_retry("judge"):
            run.note_rejected("judge_errors_first_try", verdict.errors, verdict.sparql)
            retry_feedback = self._feedback(" ".join(([CUT_OFF_FEEDBACK] if cut_off else []) + verdict.errors), verdict.sparql)
            retry_sparql, _ = await self._ask_query_agent(text, context, retry_feedback, "query_retry", run)
            retry_verdict = await asyncio.to_thread(self.judge.check, retry_sparql, text)
            run.note_repairs(retry_verdict.repairs)
            # A retry that is still not clean is a fresh query, not a repaired one: keep the less bad attempt.
            if retry_verdict.errors and severity(verdict) < severity(retry_verdict):
                run.diagnostics["kept_first_try"] = retry_verdict.errors
                run.diagnostics["worse_retry_sparql"] = retry_verdict.sparql
            else:
                sparql, verdict = retry_sparql, retry_verdict
        if verdict.errors:
            run.diagnostics["judge_errors_final"] = verdict.errors
        run.timings["generate" if feedback is None else "generate_retry"] = round((time.perf_counter() - started) * 1000, 1)
        return verdict.sparql

    def _feedback(self, reason: str, rejected_sparql: str) -> str:
        """What the query agent is told on a retry: the reason and, by default, the query it rejected."""
        if not self.retry_with_previous_query:
            return reason
        return f"{reason}\nThe rejected query was:\n{rejected_sparql[:PREVIOUS_QUERY_CHARS]}"

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
