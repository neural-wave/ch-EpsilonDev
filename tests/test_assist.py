from fastapi.testclient import TestClient

from conftest import load_examples
from nl2sparql.api import create_app
from nl2sparql.assist import clarify
from nl2sparql.context.terminology import TerminologyIndex
from nl2sparql.models import CodeEntry
from test_pipeline_and_api import ScriptedQueryAgent, pipeline_with, settings

EXAMPLES = {e["id"]: e for e in load_examples()}
ICD = "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/"
# A small stand-in for the restricted index: a block with its codes, German and French labels.
INDEX = TerminologyIndex([
    CodeEntry(ICD + "E10-E14", "E10-E14", "ICD-10-GM", ("Diabetes mellitus", "Diabète sucré")),
    CodeEntry(ICD + "E10", "E10", "ICD-10-GM", ("Diabetes mellitus, Typ 1", "Diabète sucré, type 1")),
    CodeEntry(ICD + "E11", "E11", "ICD-10-GM", ("Diabetes mellitus, Typ 2", "Diabète sucré, type 2")),
    CodeEntry(ICD + "E12", "E12", "ICD-10-GM", ("Diabète sucré de malnutrition",
                                                "Diabetes mellitus in Verbindung mit Fehl- oder Mangelernährung [Malnutrition]")),
    CodeEntry(ICD + "E13", "E13", "ICD-10-GM", ("Autres diabètes sucrés précisés", "Sonstiger näher bezeichneter Diabetes mellitus")),
    CodeEntry(ICD + "E14", "E14", "ICD-10-GM", ("Diabète sucré, sans précision", "Nicht näher bezeichneter Diabetes mellitus")),
    CodeEntry(ICD + "A00", "A00", "ICD-10-GM", ("Cholera", "Choléra")),
    CodeEntry(ICD + "A00.1", "A00.1", "ICD-10-GM", ("Cholera durch Vibrio cholerae O:1, Biovar eltor",)),
])


def test_a_family_of_codes_asks_which_kind():
    found = clarify("How many patients have diabetes?", INDEX)
    assert found.prompt == "Which kind of Diabetes mellitus?"
    labels = [choice.label for choice in found.choices]
    assert labels[:2] == ["Diabetes mellitus, Typ 1 (E10)", "Diabetes mellitus, Typ 2 (E11)"]
    assert found.choices[1].question == "How many patients have diabetes, specifically ICD-10-GM E11?"
    assert found.choices[-1].question == "How many patients have diabetes?"  # any kind: the question as asked


def test_choice_labels_match_the_question_and_fit_a_button():
    labels = [choice.label for choice in clarify("How many patients have diabetes?", INDEX).choices]
    assert "Sonstiger näher bezeichneter Diabetes mellitus (E13)" in labels  # not the French "Autres diabètes…"
    assert all("[" not in label and len(label) <= 66 for label in labels)


def test_a_chosen_code_is_not_clarified_again():
    assert clarify("How many patients have diabetes, specifically ICD-10-GM E11?", INDEX) is None


def test_a_question_naming_the_kind_in_words_is_not_clarified():
    assert clarify("How many patients have a Billed Diagnosis for type 2 diabetes?", INDEX) is None
    assert clarify("How many patients have type 1 diabetes?", INDEX) is None


def test_a_single_code_is_specific_enough():
    assert clarify("How many patients have cholera?", INDEX) is None


def test_a_count_without_a_unit_offers_full_questions():
    found = clarify("How many have cholera?", INDEX)
    assert found.prompt == "Did you mean…"
    assert [choice.question for choice in found.choices] == [
        "How many patients have cholera?",
        "How many hospital stays were there for patients who have cholera?",
    ]
    assert all(choice.label == choice.question for choice in found.choices)
    assert clarify("How many cholera diagnoses are there?", INDEX) is None


def test_other_count_wordings_get_the_unit_appended():
    found = clarify("Number of cholera?", INDEX)
    assert [choice.question for choice in found.choices] == [
        "Number of cholera, counting patients?", "Number of cholera, counting hospital stays?"]


def demo_client(*queries):
    pipeline = pipeline_with(ScriptedQueryAgent(*queries))
    pipeline.context_builder.terminology = INDEX
    return TestClient(create_app(pipeline=pipeline, settings=settings()))


def test_demo_clarify_endpoint():
    client = demo_client()
    body = client.post("/demo/clarify", json={"text": "How many patients have diabetes?"}).json()
    assert body["prompt"] == "Which kind of Diabetes mellitus?" and len(body["choices"]) == 5
    assert client.post("/demo/clarify", json={"text": "How many patients have cholera?"}).json() == {"prompt": None, "choices": []}
    assert client.post("/demo/clarify", json={"text": "  "}).status_code == 400


def test_the_judged_endpoint_is_unchanged():
    body = demo_client(EXAMPLES["spec-01"]["sparql"]).post("/nl-to-sparql", json={"text": "How many patients have diabetes?"}).json()
    assert set(body) == {"sparql_query", "tokens_consumed", "is_compliant"}
