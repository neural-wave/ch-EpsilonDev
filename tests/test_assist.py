import pytest
from fastapi.testclient import TestClient

from conftest import DATA_DIR, load_examples
from nl2sparql.api import create_app
from nl2sparql.assist import clarify, follow_ups, single_count
from nl2sparql.context.schema import SchemaCards
from nl2sparql.context.terminology import TerminologyIndex
from nl2sparql.models import CodeEntry
from nl2sparql.virtuoso import SelectResult
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


SCHEMA = SchemaCards.load(DATA_DIR / "schema_cards.json")


def labels(found):
    return [follow_up.label for follow_up in found]


def test_follow_ups_for_a_single_count_above_zero():
    found = follow_ups("How many patients have cholera?", [{"n": "767"}], SCHEMA)
    assert labels(found) == ["By sex", "By age group", "By year", "Only since 2020", "By insurance type"]
    by_sex = found[0]
    assert by_sex.question == "How many patients have cholera, split by sex (male, female, indeterminate)?"
    assert by_sex.intro == "The patients having cholera, split by sex, are:"
    assert found[3].intro is None  # a filter: its answer is one number


def test_no_follow_ups_for_zero_one_empty_or_split_answers():
    assert follow_ups("How many patients have cholera?", [{"n": "0"}], SCHEMA) == []
    assert follow_ups("How many patients have cholera?", [{"n": "1"}], SCHEMA) == []  # would describe one patient
    assert follow_ups("How many patients have cholera?", [{"n": "2"}], SCHEMA) != []
    assert follow_ups("How many patients have cholera?", [], SCHEMA) == []
    assert follow_ups("Cholera by sex?", [{"sex": "male", "n": "3"}, {"sex": "female", "n": "2"}], SCHEMA) == []
    assert single_count([{"n": "1,024"}]) == 1024 and single_count([{"sex": "male", "n": "3"}]) is None


def test_follow_ups_skip_what_the_question_already_has():
    assert labels(follow_ups("How many men have cholera?", [{"n": "5"}], SCHEMA)) == [
        "By age group", "By year", "Only since 2020", "By insurance type"]
    assert labels(follow_ups("How many patients had cholera since 2021?", [{"n": "5"}], SCHEMA)) == [
        "By sex", "By age group", "By insurance type"]


def test_follow_ups_only_when_the_data_has_the_information():
    classes = {name: card for name, card in SCHEMA.classes.items() if name not in ("AdministrativeSex", "InsuranceStatus")}
    without = SchemaCards(classes, list(SCHEMA.schema_terms))
    assert labels(follow_ups("How many patients have cholera?", [{"n": "767"}], without)) == [
        "By age group", "By year", "Only since 2020"]


def test_other_wordings_get_a_neutral_sentence():
    found = follow_ups("What is the average age of cholera patients?", [{"avg": "54"}], SCHEMA)
    assert found[0].intro == 'Split by sex, the answer to "What is the average age of cholera patients" is:'


class FakeVirtuoso:
    """Stands in for the demo's optional database check, so tests never depend on a local Virtuoso."""
    rows: list[dict] | None = None  # None: the database is unreachable

    def __init__(self, endpoint):
        pass

    async def select(self, query):
        if FakeVirtuoso.rows is None:
            raise ConnectionError("no database")
        return SelectResult(list(FakeVirtuoso.rows[0]) if FakeVirtuoso.rows else [], FakeVirtuoso.rows, 1.0)


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setattr("nl2sparql.api.VirtuosoClient", FakeVirtuoso)
    FakeVirtuoso.rows = None
    return FakeVirtuoso


# The judge checks that a query covers the coded concepts its question names, so the scripted answer to a
# cholera question must be about cholera: spec-01 (type 2 diabetes, E11) with A00 instead.
CHOLERA_COUNT = EXAMPLES["spec-01"]["sparql"].replace("icd-10-gm/E11>", "icd-10-gm/A00>")


def test_demo_answers_carry_follow_ups_after_a_result(database):
    question = {"text": "How many patients have cholera?"}
    database.rows = [{"countResult": "767"}]
    found = demo_client(CHOLERA_COUNT).post("/demo/ask", json=question).json()
    assert found["follow_ups"][0] == {
        "label": "By sex",
        "question": "How many patients have cholera, split by sex (male, female, indeterminate)?",
        "intro": "The patients having cholera, split by sex, are:",
    }
    database.rows = [{"countResult": "0"}]
    assert demo_client(CHOLERA_COUNT).post("/demo/ask", json=question).json()["follow_ups"] == []
    database.rows = None  # no database: no result, so nothing to break down
    assert demo_client(CHOLERA_COUNT).post("/demo/ask", json=question).json()["follow_ups"] == []


def test_blocked_answers_have_no_follow_ups(database):
    database.rows = [{"countResult": "767"}]
    blocked = demo_client(EXAMPLES["official-15"]["sparql"]).post(
        "/demo/ask", json={"text": "How many patients have cholera, and which ones are they?"}).json()
    assert not blocked["is_compliant"] and blocked["follow_ups"] == []
