import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import time_on_perf  # noqa: E402
from eval_common import virtuoso_error_text  # noqa: E402
from run_query_eval import compare, execute, generated_row, rates, score, signature, summarize  # noqa: E402

from nl2sparql.models import IntentFlags, PipelineResult  # noqa: E402
from nl2sparql.virtuoso import SelectResult, VirtuosoClient, VirtuosoTimeout  # noqa: E402


def test_same_rows_in_another_column_order_and_rounding_are_exact():
    reference = [{"sex": "male", "patients": "709", "pct": "14.37"}, {"sex": "female", "patients": "666", "pct": "11.7"}]
    generated = [{"n": "666", "share": "11.70", "label": "female"}, {"n": "709", "share": "14.4", "label": "male"}]
    assert signature(reference) == signature(generated) and compare(reference, generated) == "exact"


def test_outcomes_for_wrong_empty_and_failed_queries():
    reference = [{"patients": "1894"}]
    assert compare(reference, [{"patients": "0"}]) == "same_rows"
    assert compare(reference, []) == "empty"
    assert compare(reference, [{"a": "1"}, {"a": "2"}]) == "wrong"
    assert compare(reference, None) == "error"


def test_labels_are_compared_lowercased_and_stripped():
    reference = [{"time_pattern": "continuous", "prescriptions": "5580", "patients": "440"}]
    generated = [{"pattern": " Continuous", "patients": "440", "prescriptions": "5580"}]
    assert compare(reference, generated) == "exact"


def test_concept_iris_instead_of_labels_have_the_right_numbers():
    reference = [{"feeding_mode": "feeds self", "intake_records": "792", "patients": "94"},
                 {"feeding_mode": "enteral nutrition", "intake_records": "641", "patients": "96"}]
    generated = [{"concept": "http://snomed.info/id/926364008", "patients": "96", "records": "641"},
                 {"concept": "http://snomed.info/id/289002003", "patients": "94", "records": "792"}]
    assert compare(reference, generated) == "exact_numbers"


def test_a_whole_number_matches_the_decimal_it_rounds():
    reference = [{"stays": "2000", "avg_days": "49.6", "min_days": "2", "max_days": "99"}]
    assert compare(reference, [{"stays": "2000", "avg_days": "50", "min_days": "2", "max_days": "99"}]) == "exact_numbers"
    assert compare(reference, [{"stays": "2000", "avg_days": "49", "min_days": "2", "max_days": "99"}]) == "same_rows"
    assert compare([{"avg": "49.4"}], [{"avg": "49"}]) == "exact_numbers"
    assert compare([{"avg": "49.4"}], [{"avg": "49.2"}]) == "same_rows"


def test_an_extra_generated_column_is_fine_but_a_missing_one_is_not():
    reference = [{"cholera_type": "classical cholera", "patients": "203", "billed_diagnoses": "404"},
                 {"cholera_type": "el tor cholera", "patients": "192", "billed_diagnoses": "373"}]
    generated = [{"cholera_type": "classical", "patients": "203", "diagnoses": "404", "per_patient": "199"},
                 {"cholera_type": "el tor", "patients": "192", "diagnoses": "373", "per_patient": "194.3"}]
    assert compare(reference, generated) == "exact_numbers"
    assert compare(generated, reference) == "same_rows"


def test_one_row_with_a_column_per_category_has_the_right_numbers():
    reference = [{"acquisition": "original", "patients": "1002"}, {"acquisition": "acquired", "patients": "998"}]
    assert compare(reference, [{"original_patients": "1002", "acquired_patients": "998"}]) == "exact_numbers"
    assert compare(reference, [{"original_patients": "1002", "acquired_patients": "998", "all": "2000"}]) == "exact_numbers"
    assert compare([{"original_patients": "1002", "acquired_patients": "998"}], reference) == "exact_numbers"
    assert compare([{"original_patients": "1002", "acquired_patients": "998"}], reference + [{"acquisition": "unknown", "patients": "0"}]) == "wrong"
    assert compare(reference, [{"original_patients": "1002", "acquired_patients": "900"}]) == "wrong"
    assert compare(reference, [{"original_patients": "1002"}]) == "wrong"


def test_different_numbers_are_never_rescued():
    reference = [{"cholera_type": "classical cholera", "patients": "203", "billed_diagnoses": "404"},
                 {"cholera_type": "el tor cholera", "patients": "192", "billed_diagnoses": "373"}]
    generated = [{"cholera_type": "classical cholera", "patients": "10", "diagnoses": "10", "per_patient": "100"},
                 {"cholera_type": "el tor cholera", "patients": "13", "diagnoses": "13", "per_patient": "100"}]
    assert compare(reference, generated) == "same_rows"
    assert compare([{"a": "10", "b": "20"}, {"a": "30", "b": "40"}], [{"a": "10", "b": "40"}, {"a": "30", "b": "20"}]) == "same_rows"
    assert compare([{"name": "cholera"}], [{"name": "typhoid"}]) == "same_rows"
    assert compare([{"name": "cholera", "n": "5"}], [{"name": "typhoid"}]) == "same_rows"


VIRTUOSO_400 = ("Virtuoso 37000 Error SP030: SPARQL compiler, line 11: syntax error at '|' before '{'\n\n"
                "SPARQL query:\nPREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\nSELECT ...")
PARTIAL_HEADERS = {"X-SQL-State": "S1TAT", "X-Exec-Milliseconds": "4568",
                   "X-SQL-Message": "RC...: Returning incomplete results, query interrupted by result timeout."}
EMPTY_RESULT = {"head": {"vars": ["n"]}, "results": {"bindings": []}}


def client_answering(handler) -> VirtuosoClient:
    return VirtuosoClient("http://virtuoso.test/sparql", transport=httpx.MockTransport(handler))


def test_execution_error_keeps_virtuoso_message_without_the_echoed_query():
    client = client_answering(lambda request: httpx.Response(400, text=VIRTUOSO_400))
    rows, _, error = asyncio.run(execute(client, "SELECT ..."))
    assert rows is None
    assert error == "HTTP 400 Bad Request: Virtuoso 37000 Error SP030: SPARQL compiler, line 11: syntax error at '|' before '{'"

    def time_out(request):
        raise httpx.ReadTimeout("", request=request)
    assert asyncio.run(execute(client_answering(time_out), "SELECT ..."))[2] == "ReadTimeout"
    assert len(virtuoso_error_text(RuntimeError("x" * 500))) == 300
    # Virtuoso writes some errors after the partial result it had started to send (q-013).
    after_rows = ('{ "head": { "vars": ["total"] }, "results": { "bindings": [ { "total": { "value": "2000" } },\n'
                  'Virtuoso 22023 Error SR066: Unsupported case in CONVERT (UNK_DV_TYPE -> VARCHAR)\n\nSPARQL query:\nSELECT ...')
    error = asyncio.run(execute(client_answering(lambda request: httpx.Response(500, text=after_rows)), "SELECT ..."))[2]
    assert error == "HTTP 500 Internal Server Error: Virtuoso 22023 Error SR066: Unsupported case in CONVERT (UNK_DV_TYPE -> VARCHAR)"


def test_a_partial_result_is_a_timeout_never_a_success():
    partial = client_answering(lambda request: httpx.Response(206, headers=PARTIAL_HEADERS, json=EMPTY_RESULT))
    with pytest.raises(VirtuosoTimeout, match="HTTP 206, X-SQL-State S1TAT, 4568 ms"):
        asyncio.run(partial.select("SELECT ..."))
    state_only = client_answering(lambda request: httpx.Response(200, headers={"X-SQL-State": "S1TAT"}, json=EMPTY_RESULT))
    with pytest.raises(VirtuosoTimeout):
        asyncio.run(state_only.select("SELECT ..."))
    rows, _, error = asyncio.run(execute(partial, "SELECT ..."))
    assert rows is None and error.startswith("VirtuosoTimeout: Virtuoso stopped the query") and compare([{"n": "1"}], rows) == "error"
    complete = client_answering(lambda request: httpx.Response(200, json={
        "head": {"vars": ["n"]}, "results": {"bindings": [{"n": {"type": "literal", "value": "7"}}]}}))
    assert asyncio.run(complete.select("SELECT ...")).rows == [{"n": "7"}]


class FakeVirtuoso:
    """Answers each query from a dict: rows, or an exception to raise."""

    def __init__(self, answers):
        self.answers = answers

    async def select(self, query):
        answer = self.answers[query]
        if isinstance(answer, Exception):
            raise answer
        return SelectResult(sorted({k for row in answer for k in row}), answer, 1.0)


def test_score_records_the_verdict_reason_and_virtuoso_error():
    virtuoso = FakeVirtuoso({"REF": [{"n": "5"}], "GEN": [{"n": "5"}],
                             "BAD": httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://v/sparql"),
                                                          response=httpx.Response(400, text=VIRTUOSO_400))})
    base = {"split": "dev", "question": "q?", "reference_sparql": "REF", "pipeline_error": None, "retries": 0}
    rows = asyncio.run(score([
        {**base, "id": "q-1", "sparql_query": "GEN", "is_compliant": False, "compliance_reason": "R2: grouped by a code node"},
        {**base, "id": "q-2", "sparql_query": "BAD"},
        {**base, "id": "q-3", "pipeline_error": "model server down"},
    ], virtuoso))
    assert [r["outcome"] for r in rows] == ["exact", "error", "error"]
    assert rows[0]["compliance_reason"] == "R2: grouped by a code node" and rows[1]["compliance_reason"] is None
    assert rows[1]["execution_error"].startswith("HTTP 400 Bad Request: Virtuoso 37000 Error SP030")
    assert rows[2]["execution_error"] == "model server down" and rows[2]["generated_rows"] is None


def test_generated_row_keeps_the_verdict_reason():
    result = PipelineResult(sparql_query="SELECT ...", tokens_consumed=10, is_compliant=False,
                            compliance_reason="R1: the request names a patient", suggested_rephrase=None,
                            intent=IntentFlags(), violations=["R1"], retries=1, timings_ms={"total": 2.0})
    row = generated_row({"id": "q-1", "question": "q?"}, result, 2.0)
    assert row["compliance_reason"] == "R1: the request names a patient" and row["retries"] == 1
    assert row["is_compliant"] is False and row["pipeline_error"] is None and row["sparql_query"] == "SELECT ..."


def scored(id_, outcome, split="dev", difficulty="easy"):
    return {"id": id_, "split": split, "difficulty": difficulty, "outcome": outcome, "pipeline_error": None,
            "retries": 0, "judge_errors_final": [], "tokens_by_step": {"query": 100}, "tokens_consumed": 150,
            "latency_ms": 10.0, "execution_ms": 2.0, "reference_ms": 4.0, "is_compliant": True, "diagnostics": {}}


def test_rates_and_summary_show_exact_and_right_numbers():
    rows = [scored("q-1", "exact"), scored("q-2", "exact_numbers"), scored("q-3", "same_rows"),
            scored("q-4", "error"), scored("q-5", "exact_numbers", split="holdout")]
    dev = rates(rows[:4])
    assert dev["exact_rate"] == 0.25 and dev["exact_numbers_rate"] == 0.5 and dev["executes_rate"] == 0.75
    assert dev["outcomes"] == {"exact": 1, "exact_numbers": 1, "same_rows": 1, "empty": 0, "wrong": 0, "error": 1}
    summary = summarize(rows, {"backend": "llm"})
    assert summary["exact_numbers_rate"] == 0.6 and summary["by_split"]["holdout"]["exact_numbers_rate"] == 1.0
    assert summary["not_exact"] == ["q-2", "q-3", "q-4"] and summary["execution_time_ratio_median"] == 0.5


def test_run_all_prints_only_the_last_summary_lines():
    from run_all import tail_matching

    output = "Loading model\nok q-001 exact\nXX q-002 wrong\nreport: /tmp/x.json\n" + "".join(f"ok q-{i:03} exact\n" for i in range(3, 13))
    tail = tail_matching(output)
    assert tail.count("\n") == 6 and tail.endswith("ok q-012 exact\n") and "report" not in tail and "Loading" not in tail


OK = {"rows": 3, "complete": True, "error": None}
OVERRUN = {"seconds": 60.0, "rows": None, "complete": False, "error": None}
REJECTED = {"seconds": 0.1, "rows": None, "complete": False, "error": "HTTP 400 Bad Request: Virtuoso 37000 Error SP030"}


class FakePerfClient:
    """Plays scripted outcomes per query and records runs and restarts in order."""

    def __init__(self, script):
        self.script = {query: list(outcomes) for query, outcomes in script.items()}
        self.calls = []

    def run(self, sparql):
        self.calls.append(sparql)
        return dict(self.script[sparql].pop(0))

    def restart(self):
        self.calls.append("restart")


def candidate(id_, reference, generated):
    return {"id": id_, "question": f"{id_}?", "reference_sparql": reference, "sparql_query": generated, "outcome": "exact"}


def test_an_overrun_marks_the_query_timed_out_and_restarts_virtuoso_before_the_next_query():
    client = FakePerfClient({"REF1": [{**OK, "seconds": 1.0}, {**OK, "seconds": 0.5}, {**OK, "seconds": 0.7}],
                             "GEN1": [OVERRUN],
                             "REF2": [{**OK, "seconds": 0.2}] * 3, "GEN2": [{**OK, "seconds": 0.1}] * 3})
    rows, restarts = time_on_perf.time_pairs([candidate("q-1", "REF1", "GEN1"), candidate("q-2", "REF2", "GEN2")], client, runs=2)
    assert client.calls == ["REF1"] * 3 + ["GEN1", "restart"] + ["REF2"] * 3 + ["GEN2"] * 3 and restarts == 1
    assert rows[0]["reference"] == {"seconds": 0.6, "rows": 3, "complete": True, "error": None, "runs": 2, "warm_up_seconds": 1.0}
    assert rows[0]["generated"] == {**OVERRUN, "runs": 0, "warm_up_seconds": 60.0} and rows[0]["ratio"] is None
    assert rows[1]["ratio"] == 0.5
    summary = time_on_perf.summarize(rows, {"endpoint": "http://perf.test/sparql", "runs": 2, "timeout_ms": 60000,
                                            "source_report": "query-x.json", "restarts": restarts})
    assert summary["generated_timed_out"] == ["q-1"] and summary["reference_timed_out"] == [] and summary["generated_errors"] == []
    assert summary["both_complete"] == 1 and summary["median_generated_seconds"] == 0.1 and summary["median_reference_seconds"] == 0.2
    assert summary["median_ratio_generated_over_reference"] == 0.5 and summary["slower_than_reference"] == [] and summary["restarts"] == 1


def test_an_overrun_in_a_timed_run_stops_timing_and_a_rejected_query_needs_no_restart():
    client = FakePerfClient({"REF": [{**OK, "seconds": 0.3}, {**OK, "seconds": 0.3}, OVERRUN, {**OK, "seconds": 0.3}],
                             "GEN": [REJECTED]})
    rows, restarts = time_on_perf.time_pairs([candidate("q-1", "REF", "GEN")], client, runs=3)
    assert client.calls == ["REF", "REF", "REF", "restart", "GEN"] and restarts == 1
    assert rows[0]["reference"]["complete"] is False and rows[0]["reference"]["runs"] == 1 and rows[0]["reference"]["error"] is None
    assert rows[0]["generated"]["error"].startswith("HTTP 400") and rows[0]["generated"]["runs"] == 0
    summary = time_on_perf.summarize(rows, {"restarts": restarts})
    assert summary["reference_timed_out"] == ["q-1"] and summary["generated_errors"] == ["q-1"] and summary["both_complete"] == 0


def test_a_partial_result_counts_as_a_timeout_but_needs_no_restart():
    stopped = {**OVERRUN, "seconds": 12.0, "stopped_by_server": True}
    client = FakePerfClient({"REF": [stopped], "GEN": [{**OK, "seconds": 0.3}, {**OK, "seconds": 0.3}, stopped]})
    rows, restarts = time_on_perf.time_pairs([candidate("q-1", "REF", "GEN")], client, runs=2)
    assert client.calls == ["REF", "GEN", "GEN", "GEN"] and restarts == 0
    assert rows[0]["reference"]["stopped_by_server"] and rows[0]["generated"]["stopped_by_server"]
    summary = time_on_perf.summarize(rows, {"restarts": restarts})
    assert summary["reference_timed_out"] == ["q-1"] and summary["generated_timed_out"] == ["q-1"]


def test_the_container_to_restart_is_the_one_publishing_the_endpoint_port(monkeypatch):
    listing = ("virtuoso-perf\t1112/tcp, 0.0.0.0:8891->8890/tcp, [::]:8891->8890/tcp\n"
               "hackathon-virtuoso\t0.0.0.0:1111->1111/tcp, 0.0.0.0:8890->8890/tcp\n")
    monkeypatch.setattr(time_on_perf.subprocess, "run", lambda *a, **k: type("Done", (), {"stdout": listing})())
    assert time_on_perf.find_container("http://localhost:8891/sparql") == "virtuoso-perf"
    assert time_on_perf.find_container("http://localhost:8890/sparql") == "hackathon-virtuoso"
    with pytest.raises(RuntimeError, match="port 9999"):
        time_on_perf.find_container("http://localhost:9999/sparql")


def test_the_perf_client_sends_no_anytime_timeout_and_treats_overruns_as_timeouts(monkeypatch):
    seen = []

    def fake_post(endpoint, data, headers, timeout):
        seen.append((data, timeout))
        if data["query"] == "SLOW":
            raise httpx.ReadTimeout("", request=httpx.Request("POST", endpoint))
        if data["query"] == "PARTIAL":
            return httpx.Response(206, headers=PARTIAL_HEADERS, json=EMPTY_RESULT, request=httpx.Request("POST", endpoint))
        return httpx.Response(200, json={"head": {"vars": ["n"]}, "results": {"bindings": [{"n": {"value": "1"}}]}},
                              request=httpx.Request("POST", endpoint))
    monkeypatch.setattr(time_on_perf.httpx, "post", fake_post)
    client = time_on_perf.PerfClient("http://perf.test/sparql", deadline_s=30.0)
    assert client.run("FAST")["complete"] is True and client.run("FAST")["rows"] == 1
    assert time_on_perf.overran(client.run("SLOW")) and time_on_perf.overran(client.run("PARTIAL"))
    assert all(set(data) == {"query"} and timeout == 30.0 for data, timeout in seen)


def test_known_reference_timings_are_reused_only_for_the_same_reference_query(tmp_path):
    source = {"summary": {}, "rows": [{"id": "q-1", "reference_sparql": "REF1"}, {"id": "q-2", "reference_sparql": "OLD-REF2"}]}
    (tmp_path / "query-20261017-000000.json").write_text(json.dumps(source), encoding="utf-8")
    earlier = {"summary": {"source_report": "query-20261017-000000.json"},
               "rows": [{"id": "q-1", "reference": {**OK, "seconds": 4.0, "runs": 1}},
                        {"id": "q-2", "reference": {**OK, "seconds": 9.0, "runs": 1}}]}
    perf_path = tmp_path / "perf-20261017-000000.json"
    perf_path.write_text(json.dumps(earlier), encoding="utf-8")
    known = time_on_perf.known_reference_times([str(perf_path)], tmp_path)
    assert known["q-1"][0] == "REF1" and known["q-1"][1]["seconds"] == 4.0 and known["q-1"][1]["reused_from"] == perf_path.name

    client = FakePerfClient({"GEN1": [{**OK, "seconds": 0.1}] * 2, "REF2": [{**OK, "seconds": 0.3}] * 2, "GEN2": [{**OK, "seconds": 0.2}] * 2})
    rows, restarts = time_on_perf.time_pairs([candidate("q-1", "REF1", "GEN1"), candidate("q-2", "REF2", "GEN2")], client, runs=1, known=known)
    assert "REF1" not in client.calls and "REF2" in client.calls  # q-2's reference changed since the earlier report
    assert rows[0]["reference"]["seconds"] == 4.0 and rows[0]["ratio"] == round(0.1 / 4.0, 2) and rows[1]["reference"]["seconds"] == 0.3
    assert restarts == 0
