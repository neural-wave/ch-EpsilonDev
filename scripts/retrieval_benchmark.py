"""Print aggregate retrieval quality and latency for a synthetic BM25 benchmark."""
from __future__ import annotations

import statistics
import time

from nl2sparql.context.bm25 import BM25, tokenize
from nl2sparql.context.examples import ExampleBank
from nl2sparql.models import Example


EXAMPLES = [
    Example("synthetic-lab", "laboratory glucose measurement values", "SELECT * WHERE {}", True),
    Example("synthetic-birth", "premature infant delivery method", "SELECT * WHERE {}", True),
    Example("synthetic-diagnosis", "diagnosis of acute infection", "SELECT * WHERE {}", True),
    Example("synthetic-medication", "medication administration route", "SELECT * WHERE {}", True),
    Example("synthetic-age", "patient age distribution by category", "SELECT * WHERE {}", True),
    Example("synthetic-imaging", "radiology imaging examination type", "SELECT * WHERE {}", True),
]

CASES = [
    ("glucose levels measured in the lab", "synthetic-lab"),
    ("early birth delivery route", "synthetic-birth"),
    ("infection diagnosis", "synthetic-diagnosis"),
    ("drug administration pathway", "synthetic-medication"),
    ("age groups", "synthetic-age"),
    ("type of radiology exam", "synthetic-imaging"),
]

VARIANTS = ((1.5, 0.75), (1.2, 0.65), (1.8, 0.80))
TOP_K = 3
WARMUPS = 20
TIMED_RUNS = 100


def evaluate(k1: float, b: float) -> tuple[float, float, float]:
    bank = ExampleBank(EXAMPLES)
    bank.index = BM25([tokenize(example.question) for example in EXAMPLES], k1=k1, b=b)
    top1 = topk = 0
    for question, expected_id in CASES:
        found = bank.search(question, k=TOP_K)
        ids = [example.id for example in found]
        top1 += bool(ids and ids[0] == expected_id)
        topk += expected_id in ids

    for _ in range(WARMUPS):
        for question, _expected_id in CASES:
            bank.search(question, k=TOP_K)
    timings = []
    for _ in range(TIMED_RUNS):
        for question, _expected_id in CASES:
            started = time.perf_counter()
            bank.search(question, k=TOP_K)
            timings.append((time.perf_counter() - started) * 1000)

    total = len(CASES)
    return top1 / total, topk / total, statistics.median(timings)


def main() -> None:
    for k1, b in VARIANTS:
        top1, topk, p50_ms = evaluate(k1, b)
        print(f"k1={k1:.2f} b={b:.2f} top1={top1:.3f} top3={topk:.3f} p50_ms={p50_ms:.4f}")


if __name__ == "__main__":
    main()
