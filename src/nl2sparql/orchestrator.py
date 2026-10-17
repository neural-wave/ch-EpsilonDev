"""The pipeline: input check, then intent agent in parallel with context -> query agent -> judge,
then the compliance guard. The judge and the guard each have their own retry budget (MAX_RETRIES per
checker), and the judge gets one extra call when its retry was bought by syntax errors alone, so with the
default of 1 the query agent is called at most four times per request, all inside the request deadline.
With QUERY_CANDIDATES above 1, the first call is a best-of-N generation (Pipeline._best_candidate)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from nl2sparql.agents.base import IntentAgent, QueryAgent
from nl2sparql.agents.query_llm import QueryReply
from nl2sparql.context.builder import ContextBuilder
from nl2sparql.guard.decision import ComplianceGuard
from nl2sparql.input_check import normalize_text
from nl2sparql.not_a_question import NOT_A_QUESTION_REASON, is_small_talk, reads_no_data
from nl2sparql.intent_check import confirm_identifier_flag
from nl2sparql.judge import Judge, severity
from nl2sparql.models import Context, IntentFlags, JudgeResult, PipelineResult

logger = logging.getLogger(__name__)

# Added to the retry feedback when the query agent's answer hit its max_tokens limit mid-query.
CUT_OFF_FEEDBACK = "Your previous answer was cut off at the length limit. Write a shorter query and no explanation."
PREVIOUS_QUERY_CHARS = 2500  # how much of the rejected query the retry feedback carries


class PipelineError(RuntimeError):
    """The pipeline could not produce a query (e.g. the model server is down). The API answers 503."""


def _is_syntax_error(error: str) -> bool:
    """The judge's syntax errors (severity 3 in nl2sparql.judge.severity): the query did not parse, so none of
    the semantic checks ran on it."""
    return error.startswith("Syntax error")


@dataclass(frozen=True)
class _Attempt:
    """One answer of the query agent as the judge left it, and whether the answer was cut off at max_tokens."""

    verdict: JudgeResult
    cut_off: bool = False

    @property
    def sparql(self) -> str:
        return self.verdict.sparql

    @property
    def syntax_only(self) -> bool:
        return bool(self.verdict.errors) and all(_is_syntax_error(error) for error in self.verdict.errors)


class _Run:
    """Bookkeeping of one request: time and tokens per step, which checker asked for a retry."""

    def __init__(self, max_retries: int, deadline: float | None = None):
        # One budget per checker: a guard retry that comes back unparseable still gets the judge's retry,
        # and a judge retry that still breaks a rule still gets the guard's.
        self.retries_left = {"judge": max_retries, "guard": max_retries}
        # One extra judge call per request, when its retry was spent on syntax errors (Pipeline._generate).
        self.second_judge_retry_left = max_retries > 0
        self.deadline = deadline  # loop time by which the request must answer; None when not enforced
        self.longest_query_call = 0.0  # seconds: what one more query-agent call is expected to cost
        self.taken = {"judge": 0, "guard": 0}
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
        self._count_retry(checker)
        return True

    def take_second_judge_retry(self, now: float) -> bool:
        """The judge's extra call, unless it would not fit before the deadline: a request answers with the
        less bad attempt it has rather than time out (HTTP 503)."""
        if not self.second_judge_retry_left:
            return False
        if self.deadline is not None and now + self.longest_query_call > self.deadline:
            self.diagnostics["judge_retry_skipped"] = "deadline"
            return False
        self.second_judge_retry_left = False
        self._count_retry("judge")
        return True

    def _count_retry(self, checker: str) -> None:
        self.retries += 1
        self.taken[checker] += 1
        self.diagnostics["retry_reason"] = "+".join(
            f"{name} x{count}" if count > 1 else name for name, count in self.taken.items() if count
        )

    def note_rejected(self, key: str, problems: list[str], sparql: str) -> None:
        """What a checker rejected before its retry. `first_try_sparql` keeps the first answer; when the
        rejected query is a retry answer it gets its own key, so both retries can be reproduced."""
        self.diagnostics[key] = problems
        if self.diagnostics.setdefault("first_try_sparql", sparql) != sparql:
            self.diagnostics["rejected_retry_sparql"] = sparql

    def note_final(self, verdict: JudgeResult) -> None:
        """The judge's errors on the answer that is returned, and nothing when it is clean, whatever the
        earlier attempts had."""
        if verdict.errors:
            self.diagnostics["judge_errors_final"] = verdict.errors

    def add_tokens(self, step: str, used: int) -> None:
        self.tokens[step] = self.tokens.get(step, 0) + used

    def note_repairs(self, repairs: list[str]) -> None:
        if repairs:  # what the judge fixed without the model
            self.diagnostics.setdefault("judge_repairs", []).extend(repairs)

    def note_cut_off(self, step: str) -> None:
        self.diagnostics.setdefault("cut_off", []).append(step)

    def note_query_call(self, seconds: float) -> None:
        self.longest_query_call = max(self.longest_query_call, seconds)


class Pipeline:
    def __init__(self, intent_agent: IntentAgent, query_agent: QueryAgent, context_builder: ContextBuilder,
                 judge: Judge, guard: ComplianceGuard, max_input_chars: int = 2000, max_retries: int = 1,
                 log_path: Path | None = None, request_timeout_seconds: float = 75.0,
                 retry_with_previous_query: bool = False, query_candidates: int = 1,
                 query_candidate_temperature: float = 0.7):
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be a positive finite number.")
        if query_candidates < 1:
            raise ValueError("QUERY_CANDIDATES must be 1 or more.")
        if query_candidates > 1 and not (math.isfinite(query_candidate_temperature) and query_candidate_temperature > 0):
            raise ValueError("QUERY_CANDIDATE_TEMPERATURE must be a positive finite number.")
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
        self.query_candidates = query_candidates  # answers of the first generation; 1 = one greedy request
        self.query_candidate_temperature = query_candidate_temperature

    async def run(self, raw_text: str) -> PipelineResult:
        deadline = asyncio.get_running_loop().time() + self.request_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                result = await self._run(raw_text, deadline)
                # Check any synchronous work between the final await and the response.
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                return result
        except TimeoutError as error:
            raise PipelineError(f"Request timed out after {self.request_timeout_seconds:g} seconds.") from error

    async def _run(self, raw_text: str, deadline: float | None = None) -> PipelineResult:
        started = time.perf_counter()
        text = normalize_text(raw_text, self.max_input_chars)
        run = _Run(self.max_retries, deadline)
        if is_small_talk(text):  # "hey", "thanks", "how are you?": no model call, no query
            run.diagnostics["not_a_question"] = "small_talk"
            return await self._not_a_question(text, run, started, IntentFlags())

        intent_task = asyncio.create_task(self._classify(text, run))
        stage = "Context building"
        try:
            context = await asyncio.to_thread(self._timed, run, "context", lambda: self.context_builder.build(text))
            stage = "Query generation"
            attempt = await self._generate(text, context, None, run, intent_task)
            flags = await intent_task
        except BaseException as error:
            # Do not leave the concurrent intent call running after a failed or cancelled request.
            intent_task.cancel()
            await asyncio.gather(intent_task, return_exceptions=True)
            if not isinstance(error, Exception) or isinstance(error, PipelineError):
                raise
            raise PipelineError(f"{stage} failed: {error}") from error

        # The model's own verdict on a request it cannot translate: a query that reads no data. A flagged
        # request keeps its forbidden verdict and reason.
        if not flags.flagged and await asyncio.to_thread(reads_no_data, attempt.sparql):
            run.diagnostics["not_a_question"] = "query_reads_no_data"
            run.diagnostics["not_a_question_sparql"] = attempt.sparql
            return await self._not_a_question(text, run, started, flags)

        try:
            decision = await asyncio.to_thread(self._timed, run, "guard", lambda: self.guard.decide(attempt.sparql, flags))
        except Exception as error:
            raise PipelineError("Compliance check failed.") from error
        if decision.retry_hint and run.take_retry("guard"):
            violations = [f"{v.rule}: {v.message}" for v in decision.violations]
            run.note_rejected("guard_violations_first_try", violations, attempt.sparql)
            try:
                feedback = self._retry_feedback(attempt, [decision.retry_hint])
                candidate = await self._generate(text, context, feedback, run, intent_task)
                candidate_decision = await asyncio.to_thread(
                    self._timed, run, "guard_retry", lambda: self.guard.decide(candidate.sparql, flags)
                )
            except Exception as error:  # keep the first answer if the retry fails
                logger.warning("retry after guard failed: %s", error)
            else:
                # A retry that still breaks a rule, or does not parse, is a fresh query, not a repaired one:
                # keep the less bad attempt by the judge's verdict, as the judge's own retry does.
                if not candidate_decision.compliant and severity(attempt.verdict) < severity(candidate.verdict):
                    run.diagnostics["guard_kept_first_try"] = candidate.verdict.errors
                    run.diagnostics["guard_worse_retry_sparql"] = candidate.sparql
                else:
                    attempt, decision = candidate, candidate_decision
        run.note_final(attempt.verdict)

        run.timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        result = PipelineResult(
            sparql_query=attempt.sparql,
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

    async def _not_a_question(self, text: str, run: _Run, started: float, flags: IntentFlags) -> PipelineResult:
        """No query for a request that is not a question about the patient data (see not_a_question.py)."""
        run.timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        result = PipelineResult(
            sparql_query="",
            tokens_consumed=sum(run.tokens.values()),
            is_compliant=False,
            compliance_reason=NOT_A_QUESTION_REASON,
            suggested_rephrase=None,
            intent=flags,
            violations=[],
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
                        intent_task: asyncio.Task[IntentFlags]) -> _Attempt:
        """Query agent + judge. The first call counts as "query", any later one as "query_retry". With
        QUERY_CANDIDATES above 1 the first call asks for several candidates; the retries stay single and start
        from the kept candidate."""
        started = time.perf_counter()
        step = "query" if feedback is None else "query_retry"
        if step == "query" and self._samples_candidates():
            attempt = await self._best_candidate(text, context, run)
        else:
            attempt = await self._ask_and_judge(text, context, feedback, step, run)
        if step == "query" and await asyncio.to_thread(reads_no_data, attempt.sparql):
            return attempt  # the model says it cannot translate the request: no retry (see _run)
        # The first generation runs alongside the intent agent, but a forbidden request is never retried.
        if attempt.verdict.errors and run.can_retry("judge") and not (await intent_task).flagged and run.take_retry("judge"):
            run.note_rejected("judge_errors_first_try", attempt.verdict.errors, attempt.sparql)
            retry = await self._ask_and_judge(
                text, context, self._retry_feedback(attempt, attempt.verdict.errors), "query_retry", run
            )
            kept = self._less_bad(attempt, retry, run, "kept_first_try")
            # A retry bought by syntax errors alone is the first answer the semantic checks see. When it parses
            # but is still not clean, those hints get one more call instead of becoming the final answer
            # (13 of 56 dev requests of the 17 October run spent the judge's retry on syntax).
            if (kept is retry and attempt.syntax_only and retry.verdict.errors and not retry.syntax_only
                    and run.take_second_judge_retry(asyncio.get_running_loop().time())):
                run.diagnostics["judge_errors_first_retry"] = retry.verdict.errors
                run.diagnostics["first_retry_sparql"] = retry.sparql
                second = await self._ask_and_judge(
                    text, context, self._retry_feedback(retry, retry.verdict.errors), "query_retry", run
                )
                kept = self._less_bad(retry, second, run, "kept_first_retry")
            attempt = kept
        run.timings["generate" if feedback is None else "generate_retry"] = round((time.perf_counter() - started) * 1000, 1)
        return attempt

    @staticmethod
    def _less_bad(kept: _Attempt, retry: _Attempt, run: _Run, key: str) -> _Attempt:
        """A retry that is still not clean is a fresh query, not a repaired one: keep the less bad attempt."""
        if retry.verdict.errors and severity(kept.verdict) < severity(retry.verdict):
            run.diagnostics[key] = retry.verdict.errors
            run.diagnostics["worse_retry_sparql"] = retry.sparql
            return kept
        return retry

    def _retry_feedback(self, rejected: _Attempt, problems: list[str]) -> str:
        """The retry feedback for a rejected attempt: the cut-off note when its answer hit the answer cap,
        then what the checker found."""
        return self._feedback(" ".join(([CUT_OFF_FEEDBACK] if rejected.cut_off else []) + problems), rejected.sparql)

    def _feedback(self, reason: str, rejected_sparql: str) -> str:
        """What the query agent is told on a retry: the reason and, by default, the query it rejected."""
        if not self.retry_with_previous_query:
            return reason
        return f"{reason}\nThe rejected query was:\n{rejected_sparql[:PREVIOUS_QUERY_CHARS]}"

    async def _ask_and_judge(self, text: str, context: Context, feedback: str | None, step: str,
                             run: _Run) -> _Attempt:
        sparql, cut_off = await self._ask_query_agent(text, context, feedback, step, run)
        verdict = await asyncio.to_thread(self.judge.check, sparql, text)
        run.note_repairs(verdict.repairs)
        return _Attempt(verdict, cut_off)

    async def _ask_query_agent(self, text: str, context: Context, feedback: str | None, step: str,
                               run: _Run) -> tuple[str, bool]:
        """One query agent call. Returns (sparql, whether the answer was cut off at max_tokens)."""
        started = time.perf_counter()
        detailed = getattr(self.query_agent, "generate_detailed", None)  # LLM agents report more
        if detailed is None:
            sparql, used = await self.query_agent.generate(text, context, feedback)
            truncated = False
        else:
            reply = await detailed(text, context, feedback)
            sparql, used, truncated = reply.sparql, reply.tokens, reply.truncated
        run.note_query_call(time.perf_counter() - started)
        run.add_tokens(step, used)
        if truncated:
            run.note_cut_off(step)
        return sparql, truncated

    def _samples_candidates(self) -> bool:
        """Best-of-N needs an agent that can also sample (LLMQueryAgent); any other agent answers once."""
        return (self.query_candidates > 1 and hasattr(self.query_agent, "generate_detailed")
                and hasattr(self.query_agent, "sample_detailed"))

    async def _best_candidate(self, text: str, context: Context, run: _Run) -> _Attempt:
        """The first generation with QUERY_CANDIDATES = N above 1. The judge checks every candidate with the
        question, a cut-off one too, and the least bad is kept: the lowest severity, then a complete answer over
        a cut-off one, then the greedy answer, then the lower index. "candidates" records (severity, cut off)
        per candidate, the greedy one first, and "candidate_chosen" the kept one's index; cut_off and
        judge_repairs describe the kept candidate, as they describe the one answer without candidates."""
        attempts = []
        for index, reply in enumerate(await self._ask_for_candidates(text, context, run)):
            try:
                verdict = await asyncio.to_thread(self.judge.check, reply.sparql, text)
            except Exception as error:
                if index == 0:  # the greedy answer fails the request, as without candidates
                    raise
                logger.warning("judge failed on query candidate %d (0 is the greedy one), dropped: %s", index, error)
                continue
            attempts.append(_Attempt(verdict, reply.truncated))
        chosen = min(range(len(attempts)), key=lambda i: (severity(attempts[i].verdict), attempts[i].cut_off))
        attempt = attempts[chosen]
        run.diagnostics["candidates"] = [(severity(candidate.verdict), candidate.cut_off) for candidate in attempts]
        run.diagnostics["candidate_chosen"] = chosen
        run.note_repairs(attempt.verdict.repairs)
        if attempt.cut_off:
            run.note_cut_off("query")
        return attempt

    async def _ask_for_candidates(self, text: str, context: Context, run: _Run) -> list[QueryReply]:
        """The greedy answer (the usual request) and N-1 answers sampled at QUERY_CANDIDATE_TEMPERATURE by a second
        request sent at the same time with the same prompt. Both requests' tokens count as "query". A failed
        sampled request leaves the greedy answer alone; a failed greedy one fails the request, as without
        candidates. Both run inside the request deadline, which cancels them."""
        sampling = asyncio.ensure_future(self.query_agent.sample_detailed(
            text, context, self.query_candidates - 1, self.query_candidate_temperature))
        started = time.perf_counter()
        try:
            greedy = await self.query_agent.generate_detailed(text, context, None)
        except BaseException:
            sampling.cancel()
            await asyncio.gather(sampling, return_exceptions=True)
            raise
        # A retry is one greedy call: the greedy request's time is what the judge's extra retry is expected to cost.
        run.note_query_call(time.perf_counter() - started)
        try:
            samples = await sampling
        except Exception as error:
            logger.warning("sampled query candidates failed, the greedy answer goes on alone: %s", error)
            samples = []
        replies = [greedy, *samples]
        run.add_tokens("query", sum(reply.tokens for reply in replies))
        return replies

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
