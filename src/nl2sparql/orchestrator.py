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
        timings: dict[str, float] = {}
        text = normalize_text(raw_text, self.max_input_chars)
        retries_left = [self.max_retries]

        intent_task = asyncio.create_task(self._classify(text, timings))
        context = self._timed(timings, "context", lambda: self.context_builder.build(text))
        try:
            sparql, tokens = await self._generate(text, context, None, retries_left, timings)
        except Exception as error:
            intent_task.cancel()
            raise PipelineError(f"Query generation failed: {error}") from error
        flags, intent_tokens = await intent_task
        tokens += intent_tokens

        decision = self._timed(timings, "guard", lambda: self.guard.decide(sparql, flags))
        if decision.retry_hint and retries_left[0] > 0:
            retries_left[0] -= 1
            try:
                sparql, extra = await self._generate(text, context, decision.retry_hint, [0], timings)
                tokens += extra
                decision = self._timed(timings, "guard_retry", lambda: self.guard.decide(sparql, flags))
            except Exception as error:  # keep the first answer if the retry fails
                logger.warning("retry after guard failed: %s", error)

        timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        result = PipelineResult(
            sparql_query=sparql,
            tokens_consumed=tokens,
            is_compliant=decision.compliant,
            compliance_reason=decision.reason,
            suggested_rephrase=decision.suggested_rephrase,
            intent=flags,
            violations=[f"{v.rule}: {v.message}" for v in decision.violations],
            retries=self.max_retries - retries_left[0],
            timings_ms=timings,
        )
        self._log(text, result)
        return result

    async def _classify(self, text: str, timings: dict) -> tuple[IntentFlags, int]:
        started = time.perf_counter()
        try:
            return await self.intent_agent.classify(text)
        except Exception as error:  # the guard still checks the query, so degrade instead of failing
            logger.warning("intent agent failed, continuing without flags: %s", error)
            return IntentFlags(), 0
        finally:
            timings["intent"] = round((time.perf_counter() - started) * 1000, 1)

    async def _generate(self, text: str, context: Context, feedback: str | None, retries_left: list[int],
                        timings: dict) -> tuple[str, int]:
        started = time.perf_counter()
        sparql, tokens = await self.query_agent.generate(text, context, feedback)
        verdict = self.judge.check(sparql)
        if verdict.errors and retries_left[0] > 0:
            retries_left[0] -= 1
            sparql, extra = await self.query_agent.generate(text, context, " ".join(verdict.errors))
            tokens += extra
            verdict = self.judge.check(sparql)
        key = "generate" if feedback is None else "generate_retry"
        timings[key] = round((time.perf_counter() - started) * 1000, 1)
        return verdict.sparql, tokens

    @staticmethod
    def _timed(timings: dict, key: str, function):
        started = time.perf_counter()
        try:
            return function()
        finally:
            timings[key] = round((time.perf_counter() - started) * 1000, 1)

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
