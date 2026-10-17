import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from run_query_eval import compare, signature  # noqa: E402


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


def test_run_all_prints_only_the_last_summary_lines():
    from run_all import tail_matching

    output = "Loading model\nok q-001 exact\nXX q-002 wrong\nreport: /tmp/x.json\n" + "".join(f"ok q-{i:03} exact\n" for i in range(3, 13))
    tail = tail_matching(output)
    assert tail.count("\n") == 6 and tail.endswith("ok q-012 exact\n") and "report" not in tail and "Loading" not in tail
