import json
import os
import subprocess
import sys

import pytest

from conftest import DATA_DIR
from nl2sparql.context import ContextBuilder, ExampleBank, SchemaCards, TerminologyIndex
from nl2sparql.context.bm25 import BM25, tokenize
from nl2sparql.models import CodeEntry, Example
from scripts.build_terminology_index import load_english_labels


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
    assert len(terminology.candidates("alpha illness disease", k=20)) == 2


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
    assert "hasRank = one of sphn-individual:Principal | sphn-individual:Complementary | sphn-individual:Secondary" in found[0]
    assert "hasCode -> ICD-10-GM code (?c a ?concept; one code from the candidates to match a concept)" in found[0]


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


def test_load_english_terminology_labels(tmp_path):
    path = tmp_path / "english_labels.jsonl"
    path.write_text(
        '{"system":"ICD-10-GM","code":"E11","labels":["Type 2 diabetes mellitus"]}\n',
        encoding="utf-8",
    )
    assert load_english_labels(path) == {
        ("ICD-10-GM", "E11"): {"Type 2 diabetes mellitus"}
    }


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


def test_terminology_folds_plurals_and_snomed_tags_and_weighs_common_words():
    index = TerminologyIndex([
        CodeEntry("https://example.test/code/smoker", "T1", "synthetic", ("Smoker (finding)",)),
        CodeEntry("https://example.test/code/dialysis", "T2", "synthetic", ("Dialysis procedure (procedure)",)),
        CodeEntry("https://example.test/code/outpatient", "T3", "synthetic", ("Outpatient procedure (procedure)",)),
        *(CodeEntry(f"https://example.test/code/{n}", f"P{n}", "synthetic", (f"{kind} procedure (procedure)",))
          for n, kind in enumerate(["Biopsy", "Imaging", "Laboratory", "Surgical", "Dental"], start=1)),
    ])  # as in the real index, "procedure" is a common word there
    assert [e.code for e in index.candidates("How many patients are current smokers?")] == ["T1"]
    assert [e.code for e in index.candidates("Share of outpatient stays with a billed procedure")] == ["T3"]


def test_schema_cards_show_value_sets_and_rank_question_classes_first(tmp_path):
    import json
    value_sets = tmp_path / "value_sets.json"
    value_sets.write_text(json.dumps({"classes": {
        "CivilStatus": {"hasCode": [{"value": "snomed:1000001", "label": "Married", "count": 5}]},
        "AdverseEvent": {"hasOutcome": [{"value": "sphn-individual:Fatal", "label": "Fatal", "count": 3}]},
    }}))
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json", value_sets)
    found = cards.cards_for("What is the civil status breakdown?", k=2,
                            examples_sparql="?dx a sphn:BilledDiagnosis ; sphn:hasRank ?rank .")
    assert found[0].startswith("sphn:CivilStatus") and "code typed as one of Married snomed:1000001" in found[0]
    assert "hasOutcome = one of sphn-individual:Fatal" in cards.render("AdverseEvent")
    assert cards.cards_for("Patients whose directive says do not resuscitate")[0].startswith("sphn:ResuscitationDirective")


def test_context_builder_respects_custom_top_k_settings():
    builder = ContextBuilder(
        ExampleBank([
            Example("one", "alpha condition", "SELECT * WHERE {}", True),
            Example("two", "alpha condition", "SELECT * WHERE {}", True),
        ]),
        SchemaCards({
            "AlphaSyndrome": {"instances": 1, "patients": 1, "properties": {}},  # not "AlphaCondition": a candidate
            "BetaSyndrome": {"instances": 1, "patients": 1, "properties": {}},  # whose label restates a class name is dropped
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


def synthetic_cards(classes: dict, value_sets: dict | None = None) -> SchemaCards:
    return SchemaCards(
        {name: {"instances": 1, "patients": 1, "properties": props} for name, props in classes.items()}, [], value_sets
    )


def names(found: list[str]) -> list[str]:
    return [line.split(" [", 1)[0].removeprefix("sphn:") for line in found]


@pytest.mark.parametrize("word,expected", [
    ("stays", "stay"), ("specialties", "specialty"), ("ingredients", "ingredient"), ("diagnoses", "diagnosis"),
    ("diagnosis", "diagnosis"), ("status", "status"), ("cases", "case"), ("classes", "class"), ("followed", "follow"),
    ("billed", "bill"), ("billing", "bill"), ("died", "died"), ("age", "age"),
])
def test_stem_meets_plurals_and_verb_forms_without_touching_short_words(word, expected):
    from nl2sparql.context.schema import stem
    assert stem(word) == expected


def test_schema_cards_match_whole_words_not_substrings():
    cards = synthetic_cards({
        "Age": {"hasValue": {"kind": "literal", "datatype": "double"}},
        "LabTest": {"hasCode": {"kind": "resource"}},
        "AdministrativeCase": {"hasAdmissionDateTime": {"kind": "literal", "datatype": "dateTime"}},
        "Country": {},
    })
    # "average" is not "age" and "shortest" is not "test"; "stay" is a synonym of AdministrativeCase.
    assert names(cards.cards_for("Give the number of stays and the average, shortest and longest stay in days.")) \
        == ["AdministrativeCase"]
    assert names(cards.cards_for("Do patients with cholera stay longer in hospital than other patients?")) \
        == ["AdministrativeCase"]
    assert names(cards.cards_for("What is the age of our patients?")) == ["Age"]


def test_schema_cards_discount_words_shared_by_many_classes():
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    found = names(cards.cards_for("Which ten medical specialties see the most patients who have cholera on a hospital bill?", k=8))
    assert found[0] == "TherapeuticArea"
    assert "BilledDiagnosis" in found
    assert "MedicalDevice" not in found and "MedicalProcedure" not in found
    # Both words present still match a name made of generic parts only.
    assert names(cards.cards_for("How many medical procedures were recorded?", k=2))[0] == "MedicalProcedure"


def test_schema_cards_score_property_names_and_follow_the_link(tmp_path):
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    found = names(cards.cards_for("Which four active ingredients are prescribed to the most patients?", k=8))
    assert "Drug" in found and "Substance" in found and "DrugPrescription" in found
    # A single short property word ("day" for hasDay, "start" for hasStart) does not count.
    found = names(cards.cards_for("How long do hospital stays last? Give the shortest and longest stay in days.", k=8))
    assert "AdministrativeCase" in found and "BirthDate" not in found and "GenomicPosition" not in found


def test_schema_cards_hit_value_labels_and_pull_the_target_of_a_shown_link(tmp_path):
    value_sets = tmp_path / "value_sets.json"
    value_sets.write_text(json.dumps({"classes": {
        "VitalStatus": {"hasCode": [{"value": "snomed:1000001", "label": "Alive", "count": 5},
                                    {"value": "snomed:1000002", "label": "Lost to follow-up", "count": 2}]},
        "DataProvider": {"hasCategory": [{"value": "sphn-individual:Hospital", "label": "Hospital", "count": 9}]},
    }}), encoding="utf-8")
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json", value_sets)
    found = names(cards.cards_for("At follow-up, how many patients were found to be alive and how many were lost to follow-up?", k=8))
    assert found[:2] == ["VitalStatus", "FollowUp"] or found[:2] == ["FollowUp", "VitalStatus"]
    assert "DataProvider" not in names(cards.cards_for("How many patients were in hospital?", k=8))
    # Without labels the synonym table still brings both.
    plain = SchemaCards.load(DATA_DIR / "schema_cards.json")
    assert {"VitalStatus", "FollowUp"} <= set(names(plain.cards_for("How many patients were lost to follow-up?", k=8)))


def test_schema_cards_know_everyday_words_for_backbone_classes():
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    found = names(cards.cards_for("How many of our patients were born in a country they aren't a citizen of?", k=8))
    assert "Nationality" in found and "Birth" in found and "Country" in found
    found = names(cards.cards_for("By sex, what are the average BMI and the average systolic blood pressure?", k=8))
    assert {"BodyMassIndex", "BloodPressure", "AdministrativeSex"} <= set(found)
    found = names(cards.cards_for("Once someone is diagnosed with cancer, what usually happens next: surgery or radiotherapy?", k=8))
    assert {"OncologyDiagnosis", "OncologySurgery", "RadiotherapyProcedure"} <= set(found)
    assert names(cards.cards_for("How many patients died in 2020?", k=2)) == ["Death", "DeathDate"]


def test_schema_cards_rank_an_explicit_class_name_first_and_fill_spare_slots_with_its_links():
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    found = names(cards.cards_for("Patients with cancer who died during a hospital stay (use sphn:Consent)", k=8))
    assert found[0] == "Consent"
    found = names(cards.cards_for("How many babies are born early, and does that depend on how they were delivered?", k=8))
    assert found[0] == "Birth" and "GestationalAgeAtBirth" in found
    assert not {"SourceSystem", "DataProvider", "SubjectPseudoIdentifier"} & set(found)


def test_schema_cards_render_code_systems_targets_and_namespaces():
    cards = synthetic_cards({
        "Billed": {"hasCode": {"kind": "resource", "code_system": "CHOP"}},
        "Plain": {"hasCode": {"kind": "resource", "target": "Code", "code_system": "generic"}},
        "Mixed": {"hasCode": {"kind": "resource", "target": "Code", "code_system": "SNOMED CT",
                              "code_systems": {"SNOMED CT": 950, "generic": 50}}},
        "Variant": {"hasZygosityCode": {"kind": "resource", "code_system": "GENO"},
                    "hasTypeCode": {"kind": "resource", "code_system": "http://purl.obolibrary.org/obo/SO_"}},
        "Event": {"hasSample": {"kind": "resource", "target": "Sample", "targets": ["Sample", "Isolate"]},
                  "hasDrug": {"kind": "resource", "target": "Drug"}, "hasOld": {"kind": "resource", "target": "Code"}},
        "Wide": {"hasInput": {"kind": "resource", "targets": [f"Class{index:02d}" for index in range(30)]}},
    })
    concept = "(?c a ?concept; one code from the candidates to match a concept)"
    assert cards.render("Billed").endswith(f"hasCode -> CHOP code {concept}")
    assert cards.render("Plain").endswith("hasCode -> generic sphn:Code (no terminology type: cannot be matched by ICD/CHOP/SNOMED)")
    assert cards.render("Mixed").endswith("hasCode -> SNOMED CT code (95%, the rest generic sphn:Code; ?c a ?concept; "
                                          "one code from the candidates to match a concept)")
    variant = cards.render("Variant")
    assert f"hasTypeCode -> code in <http://purl.obolibrary.org/obo/SO_> {concept}" in variant
    assert f"hasZygosityCode -> GENO code {concept}" in variant
    assert variant.endswith(" (geno: <http://purl.obolibrary.org/obo/GENO_>)")
    # No card holds a code the model could copy as it stands, nor the inference option (the prompt has it).
    assert all("<code>" not in cards.render(name) and "inference" not in cards.render(name) for name in cards.classes)
    assert cards.render("Event").endswith("hasSample -> sphn:Sample | sphn:Isolate; hasDrug -> sphn:Drug; hasOld (link)")
    wide = cards.render("Wide")
    assert wide.count("sphn:Class") == 12 and wide.endswith("sphn:Class11 (the 12 most frequent of 30 classes)")


def test_schema_cards_render_their_own_value_sets_in_full_and_prefer_loaded_labels():
    members = {f"Area{index:02d}": 100 - index for index in range(40)}
    cards = synthetic_cards(
        {"TherapeuticArea": {"hasSpecialtyName": {"kind": "individual", "values": members}},
         "Billed": {"hasRank": {"kind": "individual", "values": {"Principal": 7, "Secondary": 3}},
                    "hasCode": {"kind": "resource", "code_system": "SNOMED CT"}}},
        {"Billed": {"hasCode": [{"value": "snomed:1000001", "label": "Something", "count": 7}]}},
    )
    line = cards.render("TherapeuticArea")
    assert line.count("sphn-individual:Area") == 40 and "sphn-individual:Area00 | sphn-individual:Area01" in line
    assert cards.render("Billed") == (
        "sphn:Billed [1 records, 1 patients]: hasRank = one of sphn-individual:Principal | sphn-individual:Secondary; "
        "hasCode -> code typed as one of Something snomed:1000001"
    )
    assert cards.value_sets["Billed"]["hasRank"] == [
        {"value": "sphn-individual:Principal", "label": "Principal", "count": 7},
        {"value": "sphn-individual:Secondary", "label": "Secondary", "count": 3},
    ]


@pytest.mark.parametrize("info", [
    '{"kind": "resource", "targets": "Sample"}',
    '{"kind": "resource", "targets": ["Sample", 3]}',
    '{"kind": "individual", "values": ["Principal"]}',
    '{"kind": "individual", "values": {"Principal": "7"}}',
    '{"kind": "resource", "code_system": 3}',
    '{"kind": "resource", "code_systems": {"CHOP": true}}',
])
def test_schema_loader_rejects_malformed_new_fields(tmp_path, info):
    path = tmp_path / "schema.json"
    path.write_text('{"classes": {"Example": {"instances": 1, "patients": 1, "properties": {"hasX": %s}}}, "schema_terms": []}' % info,
                    encoding="utf-8")
    with pytest.raises(ValueError, match=rf"{path}.*hasX"):
        SchemaCards.load(path)


def test_schema_loader_keeps_old_cards_and_fixtures_loading(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"classes": {"Old": {"instances": 1, "patients": 1, "properties": {
        "hasCode": {"kind": "resource", "count": 1, "target": "Code"},
        "hasRank": {"kind": "individual", "count": 1},
    }}}, "schema_terms": ["Old"]}), encoding="utf-8")
    cards = SchemaCards.load(path)
    assert cards.render("Old") == "sphn:Old [1 records, 1 patients]: hasCode (link); hasRank (value set)"


def test_regenerated_cards_state_the_terminology_links_and_value_sets_of_the_data():
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    properties = {name: card["properties"] for name, card in cards.classes.items()}
    assert properties["BilledDiagnosis"]["hasCode"]["code_system"] == "ICD-10-GM"
    assert properties["OncologyDiagnosis"]["hasCode"]["code_system"] == "ICD-10-GM"
    assert properties["BilledProcedure"]["hasCode"]["code_system"] == "CHOP"
    assert properties["LabTest"]["hasCode"]["code_system"] == "SNOMED CT"
    generic = sorted(name for name, props in properties.items() if props.get("hasCode", {}).get("code_system") == "generic")
    assert len(generic) == 17 and {"Diagnosis", "NursingDiagnosis", "MedicalProcedure", "AdverseEvent", "LabResult"} <= set(generic)
    assert all(properties[name]["hasCode"]["target"] == "Code" for name in generic)
    assert properties["Substance"]["hasCode"]["code_systems"] == {"SNOMED CT": 62420, "generic": 3022}
    assert properties["LabTestEvent"]["hasSample"]["targets"] == ["Sample", "Isolate", "TumorSpecimen"]
    assert properties["LabTestEvent"]["hasSample"]["target"] == "Sample"
    assert len(properties["TherapeuticArea"]["hasSpecialtyName"]["values"]) == 64
    assert list(properties["BilledDiagnosis"]["hasRank"]["values"]) == ["Principal", "Complementary", "Secondary"]
    assert "hasSpecialtyName = one of sphn-individual:Urology | " in cards.render("TherapeuticArea")
    assert "hasRank" in properties["BilledDiagnosis"] and "hasRankCode" not in properties["BilledDiagnosis"]


def test_profile_card_properties_measure_terminologies_targets_and_value_sets():
    from scripts.profile_data import card_properties, terminology
    sphn = "https://biomedit.ch/rdf/sphn-schema/sphn#"
    individual = "https://biomedit.ch/rdf/sphn-schema/sphn/individual#"
    kinds = {f"{sphn}hasCode": {"kind": "resource"}, f"{sphn}hasRank": {"kind": "individual"},
             f"{sphn}hasSample": {"kind": "resource"}, f"{sphn}hasDate": {"kind": "literal", "datatype": "http://www.w3.org/2001/XMLSchema#dateTime"}}
    properties = [{"p": f"{sphn}hasSample", "n": "4"}, {"p": f"{sphn}hasCode", "n": "10"},
                  {"p": f"{sphn}hasRank", "n": "3"}, {"p": f"{sphn}hasDate", "n": "2"}]
    types = [{"p": f"{sphn}hasCode", "type": "http://snomed.info/id/248153007", "n": "9"},
             {"p": f"{sphn}hasCode", "type": f"{sphn}Code", "n": "1"},
             {"p": f"{sphn}hasSample", "type": f"{sphn}Isolate", "n": "3"},
             {"p": f"{sphn}hasSample", "type": f"{sphn}Sample", "n": "1"},
             {"p": f"{sphn}hasOther", "type": f"{sphn}Sample", "n": "1"}]
    individuals = [{"p": f"{sphn}hasRank", "o": f"{individual}Secondary", "n": "1"},
                   {"p": f"{sphn}hasRank", "o": f"{individual}Principal", "n": "2"}]
    assert card_properties(properties, types, individuals, kinds) == {
        "hasCode": {"kind": "resource", "count": 10, "target": "Code", "code_system": "SNOMED CT",
                    "code_systems": {"SNOMED CT": 9, "generic": 1}},
        "hasDate": {"kind": "literal", "count": 2, "datatype": "dateTime"},
        "hasRank": {"kind": "individual", "count": 3, "values": {"Principal": 2, "Secondary": 1}},
        "hasSample": {"kind": "resource", "count": 4, "target": "Isolate", "targets": ["Isolate", "Sample"]},
    }
    assert terminology("https://biomedit.ch/rdf/sphn-resource/chop/2016/00.03") == "CHOP"
    assert terminology("https://biomedit.ch/rdf/sphn-resource/icd-10-gm/A00") == "ICD-10-GM"
    assert terminology(f"{sphn}Code") == "generic" and terminology(f"{sphn}Sample") is None
    assert terminology("http://purl.obolibrary.org/obo/SO_0001483") == "http://purl.obolibrary.org/obo/SO_"


def test_value_set_builder_caps_coded_sets_only():
    from scripts.build_value_sets import MAX_VALUES, keep
    individual = "https://biomedit.ch/rdf/sphn-schema/sphn/individual#"
    assert MAX_VALUES == 20  # takes in Intent.hasCode (17 members) and TumorGradeAssessment.hasCode (18)
    assert keep({f"{individual}Area{index}": 1 for index in range(64)})
    assert keep({f"http://snomed.info/id/{index}": 1 for index in range(MAX_VALUES)})
    assert not keep({f"http://snomed.info/id/{index}": 1 for index in range(MAX_VALUES + 1)})
    assert not keep({f"http://snomed.info/id/{index}": 1 for index in range(MAX_VALUES)} | {f"{individual}X": 1})


# --- Round 2 (17 October): no copyable placeholder, the patient link off the cards, more synonyms,
# a value-set member among the code candidates --------------------------------------------------


def test_cards_show_the_patient_link_only_on_the_pseudo_identifier_card():
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    assert sum("hasSubjectPseudoIdentifier" in card["properties"] for card in cards.classes.values()) == 79  # the data has it
    with_link = [name for name in cards.classes if "hasSubjectPseudoIdentifier" in cards.render(name)]
    assert with_link == ["SubjectPseudoIdentifier"]
    assert cards.render("SubjectPseudoIdentifier") == (
        "sphn:SubjectPseudoIdentifier [2000 records, 2000 patients] (every record links to it with hasSubjectPseudoIdentifier): "
        "hasDataProvider -> sphn:DataProvider; hasIdentifier (string); hasSharedIdentifier (anyURI)"
    )
    assert cards.render("BloodPressure").startswith("sphn:BloodPressure [")  # a card without the link is unchanged
    assert cards.in_data("hasSubjectPseudoIdentifier")  # the judge still knows the property exists


def test_picker_takes_the_pseudo_identifier_card_from_the_question_not_from_an_example():
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    forbidden_example = ("SELECT ?g ?id WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasSubjectPseudoIdentifier ?pid . "
                         "?pid a sphn:SubjectPseudoIdentifier ; sphn:hasIdentifier ?id . } }")
    found = names(cards.cards_for("How many patients have cholera on a hospital bill, by sex?", k=8, examples_sparql=forbidden_example))
    assert "BilledDiagnosis" in found and "SubjectPseudoIdentifier" not in found
    for question in ("List the pseudo identifiers of patients with type 2 diabetes.",
                     "Give me the administrative case identifiers of all cholera admissions.",
                     "Which patient IDs have a cholera diagnosis?"):
        assert "SubjectPseudoIdentifier" in names(cards.cards_for(question, k=8)), question
    assert "SubjectPseudoIdentifier" not in names(cards.cards_for("How many patients have cholera, and which ones are they?", k=8))


@pytest.mark.parametrize("question,expected,unexpected", [
    ("What are the most common ways we give drugs, how long do those treatments last, and how many start while the "
     "patient is still in hospital?", {"DrugAdministrationEvent", "AdministrativeCase"}, set()),
    ("How many babies are born early, and does that depend on how they were delivered?", {"Birth", "GestationalAgeAtBirth"}, set()),
    ("Which preterm births had a gestational age below 32 weeks?", {"GestationalAgeAtBirth"}, set()),
    ("How many patients were given medication by mouth, and how many by IV?", {"DrugAdministrationEvent"}, set()),
    ("Which administration routes are used most?", {"DrugAdministrationEvent"}, set()),
    ("How many inpatients had an adverse event?", {"AdministrativeCase", "AdverseEvent"}, set()),
    ("How many procedures were done with a diagnostic intent, and how many for screening?", {"Intent"}, set()),
    ("How many imaging procedures were done to guide another procedure?", {"ImagingProcedure", "Intent"}, set()),
    # An imperative "give", or "early" in another sense, is no reason for a card.
    ("Give the number of stays and the average, shortest and longest stay in days.", {"AdministrativeCase"}, {"DrugAdministrationEvent"}),
    ("Give me all the cancer diagnoses as a graph I can take away.", {"OncologyDiagnosis"}, {"DrugAdministrationEvent"}),
])
def test_picker_synonyms_for_administration_gestational_age_inpatients_and_intent(question, expected, unexpected):
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json")
    found = set(names(cards.cards_for(question, k=8)))
    assert expected <= found and not (unexpected & found), found


def test_a_code_candidate_that_is_a_value_set_member_pulls_its_class_through_a_shown_card(tmp_path):
    value_sets = tmp_path / "value_sets.json"
    value_sets.write_text(json.dumps({"classes": {
        "Intent": {"hasCode": [{"value": "snomed:1000001", "label": "Screening - procedure intent", "count": 5},
                               {"value": "snomed:1000002", "label": "Other", "count": 2}]},
        "DrugAdministrationEvent": {"hasReasonToStopCode": [{"value": "snomed:1000003", "label": "Lost to follow-up", "count": 1},
                                                            {"value": "snomed:1000002", "label": "Other", "count": 1}]},
        "CopyNumberVariation": {"hasTypeCode": [{"value": "<http://purl.obolibrary.org/obo/SO_0001742>", "label": "copy_number_gain", "count": 3}]},
    }}), encoding="utf-8")
    cards = SchemaCards.load(DATA_DIR / "schema_cards.json", value_sets)
    screening = CodeEntry("http://snomed.info/id/1000001", "1000001", "SNOMED CT", ("Screening - procedure intent (qualifier value)",))
    other = CodeEntry("http://snomed.info/id/1000002", "1000002", "SNOMED CT", ("Other (qualifier value)",))
    lost = CodeEntry("http://snomed.info/id/1000003", "1000003", "SNOMED CT", ("Lost to follow-up (finding)",))
    gain = CodeEntry("http://purl.obolibrary.org/obo/SO_0001742", "SO_0001742", "SO", ("copy_number_gain",))
    question = "How many imaging procedures were done for staging, and how many patients had each kind?"
    assert names(cards.cards_for(question, k=2)) == ["ImagingProcedure", "AdministrativeCase"]  # a spare slot
    assert names(cards.cards_for(question, k=2, codes=[screening])) == ["ImagingProcedure", "Intent"]  # hasIntent -> Intent
    assert names(cards.cards_for(question, k=2, codes=[other])) == ["ImagingProcedure", "AdministrativeCase"]  # "Other" names nothing
    # A member of a class no shown card links to scores nothing on its own.
    follow_up = "How many patients were followed up by telephone?"
    assert "DrugAdministrationEvent" not in names(cards.cards_for(follow_up, k=8, codes=[lost, other]))
    # A full IRI in the value set is matched too (an OBO code typed on a copy-number variation).
    variants = "How many variant descriptors carry a gain?"
    assert names(cards.cards_for(variants, k=2, codes=[gain])) == ["VariantDescriptor", "CopyNumberVariation"]
    assert "CopyNumberVariation" not in names(cards.cards_for(variants, k=2))
    assert cards.cards_for(question, k=8, codes=[]) == cards.cards_for(question, k=8)


def test_context_builder_hands_class_names_to_the_index_and_candidates_to_the_picker(tmp_path):
    value_sets = tmp_path / "value_sets.json"
    value_sets.write_text(json.dumps({"classes": {"Intent": {"hasCode": [
        {"value": "snomed:373825000", "label": "Staging - procedure intent", "count": 1}]}}}), encoding="utf-8")
    builder = ContextBuilder(
        ExampleBank([]),
        SchemaCards.load(DATA_DIR / "schema_cards.json", value_sets),
        TerminologyIndex([
            CodeEntry("http://snomed.info/id/363679005", "363679005", "SNOMED CT", ("Imaging (procedure)",)),
            CodeEntry("http://snomed.info/id/373825000", "373825000", "SNOMED CT", ("Staging - procedure intent (qualifier value)",)),
        ]),
        k_cards=2,
    )
    context = builder.build("How many imaging procedures were done for staging?")
    assert [entry.code for entry in context.code_candidates] == ["373825000"]  # "Imaging (procedure)" restates the class
    assert [card.split(" ", 1)[0] for card in context.schema_cards] == ["sphn:ImagingProcedure", "sphn:Intent"]
