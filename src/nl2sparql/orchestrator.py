"""The pipeline: input check, then intent agent in parallel with context -> query agent -> judge,
then the compliance guard. One retry per request, shared by the judge and the guard."""
from __future__ import annotations

import asyncio
import json
import logging
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


class Pipeline:
    def __init__(self, intent_agent: IntentAgent, query_agent: QueryAgent, context_builder: ContextBuilder,
                 judge: Judge, guard: ComplianceGuard, max_input_chars: int = 2000, max_retries: int = 1,
                 log_path: Path | None = None):
        self.intent_agent = intent_agent
        self.query_agent = query_agent
        self.context_builder = context_builder
        self.judge = judge
        self.guard = guard
        self.max_input_chars = max_input_chars
        self.max_retries = max_retries
        self.log_path = log_path

    async def run(self, raw_text: str) -> PipelineResult:
        started = time.perf_counter()
        text = normalize_text(raw_text, self.max_input_chars)
        run = _Run(self.max_retries)

        intent_task = asyncio.create_task(self._classify(text, run))
        context = self._timed(run, "context", lambda: self.context_builder.build(text))
        try:
            sparql = await self._generate(text, context, None, run)
        except Exception as error:
            intent_task.cancel()
            raise PipelineError(f"Query generation failed: {error}") from error
        flags = await intent_task

        decision = self._timed(run, "guard", lambda: self.guard.decide(sparql, flags))
        if decision.retry_hint and run.take_retry("guard"):
            run.diagnostics["guard_violations_first_try"] = [f"{v.rule}: {v.message}" for v in decision.violations]
            try:
                sparql = await self._generate(text, context, decision.retry_hint, run)
                decision = self._timed(run, "guard_retry", lambda: self.guard.decide(sparql, flags))
            except Exception as error:  # keep the first answer if the retry fails
                logger.warning("retry after guard failed: %s", error)

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
        self._log(text, result)
        return result

    async def _classify(self, text: str, run: _Run) -> IntentFlags:
        started = time.perf_counter()
        try:
            flags, used = await self.intent_agent.classify(text)
            run.add_tokens("intent", used)
            return flags
        except Exception as error:  # the guard still checks the query, so degrade instead of failing
            logger.warning("intent agent failed, continuing without flags: %s", error)
            run.diagnostics["intent_error"] = str(error)[:300]
            return IntentFlags()
        finally:
            run.timings["intent"] = round((time.perf_counter() - started) * 1000, 1)

    async def _generate(self, text: str, context: Context, feedback: str | None, run: _Run) -> str:
        """Query agent + judge. The first call counts as "query", any later one as "query_retry"."""
        started = time.perf_counter()
        step = "query" if feedback is None else "query_retry"
        sparql, used = await self.query_agent.generate(text, context, feedback)
        run.add_tokens(step, used)
        verdict = self.judge.check(sparql)
        if verdict.errors and run.take_retry("judge"):
            run.diagnostics["judge_errors_first_try"] = verdict.errors
            sparql, used = await self.query_agent.generate(text, context, " ".join(verdict.errors))
            run.add_tokens("query_retry", used)
            verdict = self.judge.check(sparql)
        if verdict.errors:
            run.diagnostics["judge_errors_final"] = verdict.errors
        run.timings["generate" if feedback is None else "generate_retry"] = round((time.perf_counter() - started) * 1000, 1)
        return verdict.sparql

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
