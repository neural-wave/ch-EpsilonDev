"""Print aggregate retrieval quality and latency for a synthetic BM25 benchmark."""
from __future__ import annotations

import statistics
import time

from nl2sparql.context.bm25 import BM25, tokenize
from nl2sparql.context.builder import ContextBuilder
from nl2sparql.context.examples import ExampleBank
from nl2sparql.context.schema import SchemaCards
from nl2sparql.context.terminology import TerminologyIndex
from nl2sparql.agents.prompts import query_user_prompt
from nl2sparql.models import CodeEntry, Example


EXAMPLES = [
    Example("synthetic-lab", "laboratory glucose measurement values", "SELECT * WHERE {}", True),
    Example("synthetic-birth", "premature infant delivery method", "SELECT * WHERE {}", True),
    Example("synthetic-diagnosis", "diagnosis of acute infection", "SELECT * WHERE {}", True),
    Example("synthetic-medication", "medication administration route", "SELECT * WHERE {}", True),
    Example("synthetic-age", "patient age distribution by category", "SELECT * WHERE {}", True),
    Example("synthetic-imaging", "radiology imaging examination type", "SELECT * WHERE {}", True),
    Example("synthetic-sjogren", "Sjögren syndrome diagnosis", "SELECT * WHERE {}", True),
]

CASES = [
    ("glucose levels measured in the lab", "synthetic-lab"),
    ("lab glucose measurement values", "synthetic-lab"),
    ("early birth delivery route", "synthetic-birth"),
    ("premature infant delivery method", "synthetic-birth"),
    ("infection diagnosis", "synthetic-diagnosis"),
    ("acute infection diagnosis", "synthetic-diagnosis"),
    ("drug administration pathway", "synthetic-medication"),
    ("medication administration route", "synthetic-medication"),
    ("age groups", "synthetic-age"),
    ("patient age distribution by category", "synthetic-age"),
    ("type of radiology exam", "synthetic-imaging"),
    ("radiology imaging examination type", "synthetic-imaging"),
    ("Sjogren syndrome diagnosis", "synthetic-sjogren"),
    ("glucose measurment", "synthetic-lab"),
]

VARIANTS = ((1.5, 0.75), (1.2, 0.65), (1.8, 0.80))
TOP_K = 3
WARMUPS = 20
TIMED_RUNS = 100


def context_sizes() -> tuple[int, int]:
    cards = SchemaCards({
        f"SyntheticClass{index}": {
            "instances": 100 + index,
            "patients": 50 + index,
            "properties": {f"hasSyntheticValue{index}": {"kind": "literal", "count": 100 + index}},
        }
        for index in range(8)
    }, [])
    terminology = TerminologyIndex([
        CodeEntry(
            f"https://example.test/synthetic/{index}",
            f"SYN{index}",
            "synthetic",
            (f"synthetic category {index}",),
        )
        for index in range(8)
    ])
    typical_builder = ContextBuilder(ExampleBank(EXAMPLES), cards, terminology)
    long_sparql = "SELECT * WHERE {\n" + "\n".join(
        f"?synthetic_subject_{index} <urn:synthetic:property_{index}> ?synthetic_object_{index} ."
        for index in range(50)
    ) + "\n}"
    expanded_examples = ExampleBank([
        Example(
            f"synthetic-expanded-{index}",
            "synthetic diagnosis category " + "synthetic context term " * 40,
            long_sparql,
            True,
        )
        for index in range(8)
    ])
    large_builder = ContextBuilder(
        expanded_examples, cards, terminology, k_examples=8, k_cards=8, k_codes=8
    )
    text = "synthetic diagnosis category"
    typical = typical_builder.build(text)
    large = large_builder.build(text)
    return (
        len(query_user_prompt(text, typical, None)),
        len(query_user_prompt(text, large, None)),
    )


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
    typical_chars, large_chars = context_sizes()
    for k1, b in VARIANTS:
        top1, topk, p50_ms = evaluate(k1, b)
        print(
            f"k1={k1:.2f} b={b:.2f} top1={top1:.3f} top3={topk:.3f} p50_ms={p50_ms:.4f} "
            f"context_chars_typical={typical_chars} context_chars_large={large_chars}"
        )


if __name__ == "__main__":
    main()
