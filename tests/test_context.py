import json
import os
import subprocess
import sys

import pytest

from conftest import DATA_DIR
from nl2sparql.context import ContextBuilder, ExampleBank, SchemaCards, TerminologyIndex
from nl2sparql.context.bm25 import BM25, tokenize
from nl2sparql.models import CodeEntry, Example


def test_tokenize_folds_accents_and_drops_stopwords():
    assert tokenize("How many patients have Sjögren?") == ["patients", "sjogren"]


def test_tokenize_handles_punctuation_and_bm25_handles_repeated_terms_and_empty_corpus():
    assert tokenize("alpha-beta!") == ["alpha", "beta"]
    assert BM25([]).scores(["alpha"]) == []
    score = BM25([["alpha", "beta"]]).scores(["alpha", "alpha"])[0]
    assert score > BM25([["alpha", "beta"]]).scores(["alpha"])[0]


def test_example_bank_finds_similar_question():
    bank = ExampleBank.load(DATA_DIR / "examples.jsonl")
    top = bank.search("How many babies are born early by delivery mode?", k=1)
    assert top[0].id == "official-10"


def test_example_bank_can_exclude_the_exact_question():
    bank = ExampleBank.load(DATA_DIR / "examples.jsonl")
    question = bank.examples[0].question
    assert bank.search(question, k=1)[0].question == question
    assert bank.search(question, k=1, exclude_exact=True)[0].question != question


def test_example_bank_exact_exclusion_normalizes_case_accents_and_whitespace():
    example = Example("synthetic", "How many Sjögren records?", "SELECT * WHERE {}", True)
    bank = ExampleBank([example])
    assert bank.search(" HOW   MANY sjogren   records? ", k=1, exclude_exact=True) == []


@pytest.mark.parametrize("k", [0, -1, -10])
def test_retrievers_return_no_results_for_nonpositive_k(k):
    example_bank = ExampleBank([
        Example("one", "alpha question", "SELECT * WHERE {}", True),
        Example("two", "beta question", "SELECT * WHERE {}", True),
    ])
    cards = SchemaCards({
        "AlphaCondition": {"instances": 1, "patients": 1, "properties": {}},
        "BetaCondition": {"instances": 1, "patients": 1, "properties": {}},
    }, [])
    terminology = TerminologyIndex([
        CodeEntry("https://example.test/code/one", "A00", "synthetic", ("alpha illness",)),
        CodeEntry("https://example.test/code/two", "B00", "synthetic", ("beta illness",)),
    ])
    assert example_bank.search("alpha", k=k) == []
    assert cards.cards_for("condition", k=k) == []
    assert terminology.candidates("alpha illness", k=k) == []


def test_retrievers_return_all_results_when_k_exceeds_collection_size():
    examples = ExampleBank([
        Example("one", "alpha question", "SELECT * WHERE {}", True),
        Example("two", "alpha query", "SELECT * WHERE {}", True),
    ])
    cards = SchemaCards({
        "AlphaCondition": {"instances": 1, "patients": 1, "properties": {}},
        "BetaCondition": {"instances": 1, "patients": 1, "properties": {}},
    }, [])
    terminology = TerminologyIndex([
        CodeEntry("https://example.test/code/one", "A00", "synthetic", ("alpha illness",)),
        CodeEntry("https://example.test/code/two", "B00", "synthetic", ("alpha disease",)),
    ])
    assert len(examples.search("alpha", k=20)) == 2
    assert len(cards.cards_for("condition", k=20)) == 2
    assert len(terminology.candidates("alpha", k=20)) == 2


@pytest.mark.parametrize("text", ["", "the and is", "   HOW the IS   "])
def test_empty_or_stopword_only_queries_return_no_retrieval_matches(text):
    examples = ExampleBank([Example("one", "alpha question", "SELECT * WHERE {}", True)])
    cards = SchemaCards({
        "AlphaCondition": {"instances": 1, "patients": 1, "properties": {}},
    }, [])
    terminology = TerminologyIndex([
        CodeEntry("https://example.test/code/one", "A00", "synthetic", ("alpha illness",)),
    ])
    assert examples.search(text) == []
    assert cards.cards_for(text) == []
    assert terminology.candidates(text) == []


def test_example_ties_keep_source_order():
    examples = ExampleBank([
        Example("first", "alpha beta", "SELECT * WHERE {}", True),
        Example("second", "alpha beta", "SELECT * WHERE {}", True),
    ])
    assert [entry.id for entry in examples.search("alpha", k=2)] == ["first", "second"]


def test_schema_card_ties_are_alphabetical_ascending():
    cards = SchemaCards({
        "ZetaCondition": {"instances": 1, "patients": 1, "properties": {}},
        "AlphaCondition": {"instances": 1, "patients": 1, "properties": {}},
    }, [])
    found = cards.cards_for("condition", k=2)
    assert [line.split(" [", 1)[0] for line in found] == ["sphn:AlphaCondition", "sphn:ZetaCondition"]


def test_schema_cards_match_explicit_sphn_class_names():
    cards = SchemaCards({
        "AlphaCondition": {"instances": 1, "patients": 1, "properties": {}},
    }, [])
    assert cards.cards_for("sphn:AlphaCondition", k=1)[0].startswith("sphn:AlphaCondition")


def test_schema_closest_ties_follow_the_existing_sorted_candidate_choice():
    cards = SchemaCards({
        "Bat": {"instances": 1, "patients": 1, "properties": {}},
        "Hat": {"instances": 1, "patients": 1, "properties": {}},
    }, [])
    assert cards.closest("Cat") == "Hat"


def test_terminology_ties_keep_index_order_even_when_codes_are_mentioned_reversed():
    entries = [
        CodeEntry("https://example.test/code/z", "Z99", "synthetic", ("alpha illness",)),
        CodeEntry("https://example.test/code/a", "A00", "synthetic", ("beta illness",)),
    ]
    index = TerminologyIndex(entries)
    assert index.candidates("A00 and Z99", k=2) == entries


def test_repeated_context_builds_have_identical_results():
    builder = ContextBuilder(
        ExampleBank([
            Example("first", "billed diagnosis", "SELECT * WHERE {}", True),
            Example("second", "billed diagnosis", "SELECT * WHERE {}", True),
        ]),
        SchemaCards({
            "ZetaDiagnosis": {"instances": 1, "patients": 1, "properties": {}},
            "AlphaDiagnosis": {"instances": 1, "patients": 1, "properties": {}},
        }, []),
        TerminologyIndex([
            CodeEntry("https://example.test/code/z", "Z99", "synthetic", ("alpha disease",)),
            CodeEntry("https://example.test/code/a", "A00", "synthetic", ("alpha illness",)),
        ]),
    )
    assert builder.build("billed diagnosis alpha") == builder.build("billed diagnosis alpha")


def test_schema_cards_pick_named_classes():
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    found = cards.cards_for("How many patients have a billed diagnosis of cholera?", k=3)
    assert found[0].startswith("sphn:BilledDiagnosis [")
    assert "hasRank (value set)" in found[0]


def test_schema_cards_know_the_data_and_suggest_fixes():
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    assert cards.in_data("BilledDiagnosis") and cards.in_data("hasRank")
    assert not cards.in_data("hasRankCode")  # defined in the 2026.1 schema, absent from the data
    assert cards.closest("hasRankCode") == "hasRank"


def test_missing_files_give_empty_components(tmp_path):
    assert ExampleBank.load(tmp_path / "none.jsonl").search("anything") == []
    assert SchemaCards.load(tmp_path / "none.json").in_data("Anything")
    assert TerminologyIndex.load(tmp_path / "none.jsonl").candidates("cholera") == []


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{bad json}', "line 1"),
        (json.dumps({"id": "x", "question": "q", "compliant": True}), "sparql"),
        (json.dumps({"id": 1, "question": "q", "sparql": "SELECT", "compliant": True}), "id"),
    ],
)
def test_example_loader_reports_file_and_line_for_malformed_jsonl(tmp_path, payload, expected):
    path = tmp_path / "examples.jsonl"
    path.write_text(payload + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"{path}.*line 1.*{expected}"):
        ExampleBank.load(path)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{bad json}', "line 1"),
        (json.dumps({"iri": "https://example.test/x", "code": "X1", "system": "synthetic"}), "labels"),
        (json.dumps({"iri": 1, "code": "X1", "system": "synthetic", "labels": []}), "iri"),
    ],
)
def test_terminology_loader_reports_file_and_line_for_malformed_jsonl(tmp_path, payload, expected):
    path = tmp_path / "terminology.jsonl"
    path.write_text(payload + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"{path}.*line 1.*{expected}"):
        TerminologyIndex.load(path)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"schema_terms": []}',
        '{"classes": [], "schema_terms": []}',
        '{"classes": {"Example": {}}, "schema_terms": []}',
        '{"classes": {"Example": {"properties": []}}, "schema_terms": []}',
        '{"classes": {}, "schema_terms": "not a list"}',
    ],
)
def test_schema_loader_rejects_malformed_card_structures(tmp_path, payload):
    path = tmp_path / "schema.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match=rf"{path}"):
        SchemaCards.load(path)


def test_terminology_matches_codes_and_labels():
    index = TerminologyIndex([
        CodeEntry("https://biomedit.ch/rdf/sphn-resource/icd-10-gm/A00", "A00", "ICD-10-GM", ("Cholera",)),
        CodeEntry("https://biomedit.ch/rdf/sphn-resource/icd-10-gm/E11", "E11", "ICD-10-GM", ("Diabetes mellitus, Typ 2",)),
    ])
    assert [e.code for e in index.candidates("How many patients have cholera?")] == ["A00"]
    assert [e.code for e in index.candidates("Patients with E11 diagnoses")] == ["E11"]


def test_terminology_matches_accent_folded_labels_and_multiple_labels():
    index = TerminologyIndex([
        CodeEntry(
            "https://example.test/code/synthetic",
            "SYN1",
            "synthetic",
            ("Sjögren condition", "synthetic dry-eye disorder"),
        ),
    ])
    assert [entry.code for entry in index.candidates("sjogren condition")] == ["SYN1"]


def test_example_bank_preserves_duplicate_entries_and_source_order():
    examples = [
        Example("same", "alpha question", "SELECT * WHERE {}", True),
        Example("same", "alpha question", "SELECT * WHERE {}", True),
    ]
    bank = ExampleBank(examples)
    assert bank.search("alpha", k=3) == examples


def test_context_builder_combines_everything():
    builder = ContextBuilder(
        ExampleBank.load(DATA_DIR / "examples.jsonl"),
        SchemaCards.load(DATA_DIR / "schema_cards.json"),
        TerminologyIndex([]),
    )
    context = builder.build("How many patients have a billed diagnosis of cholera, by sex?")
    assert len(context.examples) == 3
    assert any(card.startswith("sphn:BilledDiagnosis") for card in context.schema_cards)


def test_context_builder_respects_custom_top_k_settings():
    builder = ContextBuilder(
        ExampleBank([
            Example("one", "alpha condition", "SELECT * WHERE {}", True),
            Example("two", "alpha condition", "SELECT * WHERE {}", True),
        ]),
        SchemaCards({
            "AlphaCondition": {"instances": 1, "patients": 1, "properties": {}},
            "BetaCondition": {"instances": 1, "patients": 1, "properties": {}},
        }, []),
        TerminologyIndex([
            CodeEntry("https://example.test/code/a", "A00", "synthetic", ("alpha condition",)),
            CodeEntry("https://example.test/code/b", "B00", "synthetic", ("alpha condition",)),
        ]),
        k_examples=1,
        k_cards=1,
        k_codes=1,
    )
    context = builder.build("alpha condition")
    assert len(context.examples) == 1
    assert len(context.schema_cards) == 1
    assert len(context.code_candidates) == 1


def test_example_context_does_not_create_unrequested_terminology_candidates():
    builder = ContextBuilder(
        ExampleBank([
            Example("example", "ordinary research question", "SELECT * WHERE { ?x <urn:synthetic> ?y }", True),
        ]),
        SchemaCards({}, []),
        TerminologyIndex([
            CodeEntry("https://example.test/code/rare", "SYN9", "synthetic", ("rare synthetic marker",)),
        ]),
    )
    context = builder.build("ordinary research question")
    assert context.examples
    assert context.code_candidates == []


def test_large_synthetic_local_files_load_without_truncation(tmp_path):
    examples_path = tmp_path / "many_examples.jsonl"
    rows = [
        json.dumps({
            "id": f"synthetic-{index}",
            "question": f"alpha synthetic question {index}",
            "sparql": "SELECT * WHERE {}",
            "compliant": True,
        })
        for index in range(1200)
    ]
    examples_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    bank = ExampleBank.load(examples_path)
    assert len(bank.examples) == 1200
    assert len(bank.search("alpha synthetic question", k=5000)) == 1200


def test_context_request_path_needs_no_virtuoso_client_or_endpoint():
    repo = DATA_DIR.parent
    env = os.environ.copy()
    env.pop("VIRTUOSO_ENDPOINT", None)
    env["PYTHONPATH"] = str(repo / "src")
    code = (
        "import sys; "
        "from nl2sparql.context import ContextBuilder, ExampleBank, SchemaCards, TerminologyIndex; "
        "builder = ContextBuilder(ExampleBank([]), SchemaCards({}, []), TerminologyIndex([])); "
        "builder.build('synthetic request'); "
        "assert 'nl2sparql.virtuoso' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True, text=True)
