from conftest import DATA_DIR
from nl2sparql.context import ContextBuilder, ExampleBank, SchemaCards, TerminologyIndex
from nl2sparql.context.bm25 import tokenize
from nl2sparql.models import CodeEntry


def test_tokenize_folds_accents_and_drops_stopwords():
    assert tokenize("How many patients have Sjögren?") == ["patients", "sjogren"]


def test_example_bank_finds_similar_question():
    bank = ExampleBank.load(DATA_DIR / "examples.jsonl")
    top = bank.search("How many babies are born early by delivery mode?", k=1)
    assert top[0].id == "official-10"


def test_example_bank_can_exclude_the_exact_question():
    bank = ExampleBank.load(DATA_DIR / "examples.jsonl")
    question = bank.examples[0].question
    assert bank.search(question, k=1)[0].question == question
    assert bank.search(question, k=1, exclude_exact=True)[0].question != question


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


def test_terminology_matches_codes_and_labels():
    index = TerminologyIndex([
        CodeEntry("https://biomedit.ch/rdf/sphn-resource/icd-10-gm/A00", "A00", "ICD-10-GM", ("Cholera",)),
        CodeEntry("https://biomedit.ch/rdf/sphn-resource/icd-10-gm/E11", "E11", "ICD-10-GM", ("Diabetes mellitus, Typ 2",)),
    ])
    assert [e.code for e in index.candidates("How many patients have cholera?")] == ["A00"]
    assert [e.code for e in index.candidates("Patients with E11 diagnoses")] == ["E11"]


def test_context_builder_combines_everything():
    builder = ContextBuilder(
        ExampleBank.load(DATA_DIR / "examples.jsonl"),
        SchemaCards.load(DATA_DIR / "schema_cards.json"),
        TerminologyIndex([]),
    )
    context = builder.build("How many patients have a billed diagnosis of cholera, by sex?")
    assert len(context.examples) == 3
    assert any(card.startswith("sphn:BilledDiagnosis") for card in context.schema_cards)


def test_terminology_folds_plurals_and_snomed_tags_and_weighs_common_words():
    index = TerminologyIndex([
        CodeEntry("http://snomed.info/id/77176002", "77176002", "SNOMED CT", ("Smoker (finding)",)),
        CodeEntry("http://snomed.info/id/108241001", "108241001", "SNOMED CT", ("Dialysis procedure (procedure)",)),
        CodeEntry("http://snomed.info/id/371883000", "371883000", "SNOMED CT", ("Outpatient procedure (procedure)",)),
    ])
    assert [e.code for e in index.candidates("How many patients are current smokers?")] == ["77176002"]
    assert [e.code for e in index.candidates("Share of outpatient stays with a billed procedure")] == ["371883000"]


def test_schema_cards_show_value_sets_and_rank_question_classes_first(tmp_path):
    import json
    value_sets = tmp_path / "value_sets.json"
    value_sets.write_text(json.dumps({"classes": {
        "CivilStatus": {"hasCode": [{"value": "snomed:87915002", "label": "Married", "count": 5}]},
        "AdverseEvent": {"hasOutcome": [{"value": "sphn-individual:Fatal", "label": "Fatal", "count": 3}]},
    }}))
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json", value_sets)
    found = cards.cards_for("What is the civil status breakdown?", k=2,
                            examples_sparql="?dx a sphn:BilledDiagnosis ; sphn:hasRank ?rank .")
    assert found[0].startswith("sphn:CivilStatus") and "code typed as one of Married snomed:87915002" in found[0]
    assert "hasOutcome = one of sphn-individual:Fatal" in cards.render("AdverseEvent")
    assert cards.cards_for("Patients whose directive says do not resuscitate")[0].startswith("sphn:ResuscitationDirective")
