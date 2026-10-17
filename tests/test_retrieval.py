"""Retrieval fixes of 17 October: example shapes and the team's retrieval-only examples (#17),
terminology candidates matched by one word, English aliases and subcodes (#16)."""
import json
import sys
from pathlib import Path

import pytest

from conftest import DATA_DIR
from nl2sparql.config import Settings
from nl2sparql.context import ExampleBank, SchemaCards, TerminologyIndex
from nl2sparql.context.examples import needed_shape
from nl2sparql.context.terminology import parent_code
from nl2sparql.judge import Judge
from nl2sparql.models import CodeEntry, Example
from nl2sparql.sparql.parse import parse_sparql
from scripts.build_terminology_index import ENGLISH_ALIASES, english_labels

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from eval_common import load_questions  # noqa: E402
from run_query_eval import load_query_questions  # noqa: E402

TEAM_IDS = ["team-ex-01", "team-ex-02", "team-ex-03"]


def shapes_of(bank: ExampleBank, examples: list[Example]) -> list[str]:
    return [bank.shapes[bank.examples.index(example)] for example in examples]


def example(identifier: str, question: str, shape: str = "plain") -> Example:
    return Example(identifier, question, f"SELECT * WHERE {{ # {shape} }}", True)


def bank_of(*rows: tuple[str, str, str]) -> ExampleBank:
    """A bank from (id, question, shape) rows."""
    return ExampleBank([example(*row) for row in rows], [row[2] for row in rows])


# --- #17: shapes and team examples -------------------------------------------------------------


def test_team_examples_load_next_to_the_official_file_with_their_shapes():
    bank = ExampleBank.load(DATA_DIR / "examples.jsonl")
    ids = [item.id for item in bank.examples]
    assert ids[:16] == [f"official-{n:02d}" for n in range(1, 16)] + ["spec-01"]
    assert ids[16:] == TEAM_IDS
    shapes = dict(zip(ids, bank.shapes))
    assert {key: shapes[key] for key in ("official-02", "official-03", "official-10", "spec-01", *TEAM_IDS)} == {
        "official-02": "two_cohort", "official-03": "conditional_count", "official-10": "breakdown",
        "spec-01": "plain", "team-ex-01": "two_cohort", "team-ex-02": "conditional_count", "team-ex-03": "breakdown",
    }
    assert all(item.source == "team" and item.compliant for item in bank.examples[16:])


def test_team_examples_never_enter_the_eval_set():
    """The eval scripts read data/examples.jsonl only: 11 compliant official questions + 76 reference
    questions for the query eval, 16 official + 15 team questions for the pipeline eval."""
    settings = Settings()
    query_ids = [row["id"] for row in load_query_questions(settings)]
    pipeline_ids = [row["id"] for row in load_questions(settings)]
    assert len(query_ids) == 87 and len(pipeline_ids) == 31
    assert not {*query_ids, *pipeline_ids} & set(TEAM_IDS)


def test_team_examples_are_valid_compliant_and_verified_on_dev(guard):
    judge = Judge(SchemaCards.load(DATA_DIR / "schema_cards.json"))
    rows = [json.loads(line) for line in (DATA_DIR / "team_examples.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["id"] for row in rows] == TEAM_IDS
    for row in rows:
        assert parse_sparql(row["sparql"]).kind == "SelectQuery", row["id"]
        assert judge.check(row["sparql"]).errors == [], row["id"]
        assert guard.check_query(row["sparql"]) == [], row["id"]
        assert "Verified on the dev database" in row["note"] and "row" in row["note"], row["id"]
        assert "DEFINE" not in row["sparql"] and "COUNT(DISTINCT ?g)" in row["sparql"]


def test_shape_tags_are_validated(tmp_path):
    path = tmp_path / "examples.jsonl"
    path.write_text(json.dumps({"id": "x", "question": "q", "sparql": "SELECT", "compliant": True, "shape": "pie"}) + "\n")
    with pytest.raises(ValueError, match=r"line 1.*shape"):
        ExampleBank.load(path)
    path.write_text(json.dumps({"id": "x", "question": "q", "sparql": "SELECT", "compliant": True, "shape": "breakdown"}) + "\n"
                    + json.dumps({"id": "y", "question": "q", "sparql": "SELECT", "compliant": True}) + "\n")
    assert ExampleBank.load(path).shapes == ["breakdown", "plain"]
    with pytest.raises(ValueError, match="one entry per example"):
        ExampleBank([example("x", "q")], ["plain", "plain"])


def test_a_team_file_next_to_a_custom_official_file_is_picked_up_and_only_once(tmp_path):
    (tmp_path / "examples.jsonl").write_text(json.dumps({"id": "o", "question": "alpha", "sparql": "SELECT", "compliant": True}) + "\n")
    (tmp_path / "team_examples.jsonl").write_text(
        json.dumps({"id": "t", "question": "beta", "sparql": "SELECT", "compliant": True, "shape": "two_cohort"}) + "\n")
    assert [item.id for item in ExampleBank.load(tmp_path / "examples.jsonl").examples] == ["o", "t"]
    assert [item.id for item in ExampleBank.load(tmp_path / "team_examples.jsonl").examples] == ["t"]
    assert ExampleBank.load(tmp_path / "missing.jsonl").shapes == ["two_cohort"]


@pytest.mark.parametrize("text,shape", [
    ("Of the patients given drugs, what share also had an adverse event?", "conditional_count"),
    ("How often does a serious adverse event end in death?", "conditional_count"),
    ("How many of our patients were born abroad?", "conditional_count"),
    ("What usually happens after a cancer diagnosis?", "conditional_count"),
    ("What percentage of patients has cholera?", "conditional_count"),
    ("What is the proportion of men among cholera patients?", "conditional_count"),
    ("What fraction of stays has a billed procedure?", "conditional_count"),
    ("Do cholera patients stay longer than other patients?", "two_cohort"),
    ("Cholera patients Compared With the rest: how many lab tests?", "two_cohort"),
    ("Men versus women: average age", "two_cohort"),
    ("men vs women by age", "two_cohort"),
    ("Do cholera patients get more prescriptions than other patients, and what share get any?", "two_cohort"),
    ("Patients with a shared identifier", None),  # "share" only as a whole word
    ("How many patients have cholera?", None),
])
def test_needed_shape_reads_the_cue_phrases(text, shape):
    assert needed_shape(text) == shape


def test_share_question_gets_one_conditional_count_example_in_the_last_slot():
    bank = bank_of(
        ("plain-1", "adverse event drugs patients hospital", "plain"),
        ("plain-2", "adverse event drugs patients", "plain"),
        ("plain-3", "adverse event patients", "plain"),
        ("plain-4", "patients", "plain"),
        ("cond-1", "something unrelated", "conditional_count"),
        ("cond-2", "adverse event", "conditional_count"),
    )
    found = bank.search("Of the patients given drugs in hospital, what share had an adverse event?", k=3)
    assert [item.id for item in found] == ["plain-1", "plain-2", "cond-2"]  # best-scoring of the shape
    assert shapes_of(bank, found) == ["plain", "plain", "conditional_count"]
    # No cue: BM25 order untouched. Shape already present: nothing swapped.
    assert [item.id for item in bank.search("adverse event drugs patients hospital", k=3)] == ["plain-1", "plain-2", "plain-3"]
    bank2 = bank_of(("cond", "adverse event share", "conditional_count"), ("plain", "adverse event", "plain"))
    assert [item.id for item in bank2.search("what share had an adverse event?", k=2)] == ["cond", "plain"]


def test_cohort_question_gets_a_two_cohort_example_and_wins_over_the_share_cue():
    bank = bank_of(
        ("plain-1", "cholera patients prescriptions", "plain"),
        ("plain-2", "cholera patients", "plain"),
        ("cond", "proportion of patients", "conditional_count"),
        ("cohort", "two groups of patients", "two_cohort"),
    )
    found = bank.search("Do cholera patients get more prescriptions than other patients, and what share get any?", k=3)
    assert [item.id for item in found] == ["plain-1", "plain-2", "cohort"]
    # The top example is never evicted: one slot keeps the BM25 winner, two slots swap the second.
    assert [item.id for item in bank.search("cholera patients versus the rest", k=1)] == ["plain-2"]
    assert [item.id for item in bank.search("cholera patients versus the rest", k=2)] == ["plain-2", "cohort"]


def test_shape_swap_respects_exclude_exact_and_k_larger_than_the_bank():
    bank = bank_of(
        ("plain", "cholera patients", "plain"),
        ("cond-same", "what share of cholera patients?", "conditional_count"),
        ("cond-other", "unrelated", "conditional_count"),
    )
    found = bank.search("what share of cholera patients?", k=2, exclude_exact=True)
    assert [item.id for item in found] == ["plain", "cond-other"]
    assert [item.id for item in bank.search("what share of cholera patients?", k=10)] == ["cond-same", "plain", "cond-other"]
    assert bank.search("", k=3) == [] and bank.search("what share?", k=0) == []


def test_real_bank_offers_the_team_examples_for_their_shapes():
    bank = ExampleBank.load(DATA_DIR / "examples.jsonl")
    found = bank.search("Do patients with a do-not-resuscitate decision stay longer than other patients?", k=3)
    assert "two_cohort" in shapes_of(bank, found)
    found = bank.search("Of the patients who were given drugs in hospital, what share also had an adverse event recorded?", k=3)
    assert "conditional_count" in shapes_of(bank, found)
    found = bank.search("Do patients with cholera on a hospital bill have more lab tests than other patients?", k=3)
    assert found[0].id == "team-ex-01"
    # The official examples keep their place for their own questions (the eval excludes them by text).
    assert bank.search("How many babies are born early by delivery mode?", k=1)[0].id == "official-10"
    assert "official-03" not in [item.id for item in bank.search(bank.examples[2].question, k=3, exclude_exact=True)]


# --- #16: terminology candidates ----------------------------------------------------------------


def diabetes_entries() -> list[CodeEntry]:
    """As in the real index: "mellitus" follows "diabetes" in nearly every entry."""
    return [CodeEntry(f"https://example.test/icd/E1{n}", f"E1{n}", "ICD-10-GM", (f"Diabetes mellitus, Typ {n}",)) for n in range(4)]


def test_one_shared_word_must_be_the_whole_label_or_imply_the_rest():
    index = TerminologyIndex([
        CodeEntry("https://example.test/snomed/16983000", "16983000", "SNOMED CT", ("Death in hospital (event)",)),
        CodeEntry("https://example.test/snomed/462621003", "462621003", "SNOMED CT", ("Hospital-administration information system (physical object)",)),
        CodeEntry("https://example.test/snomed/462621004", "462621004", "SNOMED CT", ("Hospital pharmacy (environment)",)),
        CodeEntry("https://example.test/snomed/262202000", "262202000", "SNOMED CT", ("Therapeutic intent (qualifier value)",)),
        CodeEntry("https://example.test/snomed/260353006", "260353006", "SNOMED CT", ("Therapeutic (qualifier value)",)),
        CodeEntry("https://example.test/snomed/307154001", "307154001", "SNOMED CT", ("During procedure (qualifier value)",)),
        CodeEntry("https://example.test/snomed/307168008", "307168008", "SNOMED CT", ("During admission (qualifier value)",)),
        CodeEntry("https://example.test/snomed/307168009", "307168009", "SNOMED CT", ("During operation (qualifier value)",)),
        CodeEntry("https://example.test/snomed/307168010", "307168010", "SNOMED CT", ("During exercise (qualifier value)",)),
        CodeEntry("https://example.test/snomed/371883000", "371883000", "SNOMED CT", ("Outpatient procedure (procedure)",)),
        CodeEntry("https://example.test/icd/A00", "A00", "ICD-10-GM", ("Cholera", "Choléra")),
        CodeEntry("https://example.test/icd/E10-E14", "E10-E14", "ICD-10-GM", ("Diabetes mellitus",)),
        *diabetes_entries(),
    ])
    codes = lambda text: [entry.code for entry in index.candidates(text)]  # noqa: E731
    assert codes("How long do hospital stays last?") == []
    assert codes("How many deaths in hospital were recorded?") == ["16983000"]
    assert codes("Which therapeutic ultrasound procedures do we bill?") == ["260353006"]  # the one-word label only
    assert codes("procedures with a therapeutic intent") == ["262202000", "260353006"]
    assert codes("During hospital stays, how many lab tests?") == []  # "during" is in 4 entries, "procedure" in 1
    assert codes("How many patients have cholera?") == ["A00"]
    # "diabetes" alone implies "mellitus" (5 of 5 entries) but not "1": the block code only.
    assert codes("How many patients have diabetes?") == ["E10-E14"]
    assert codes("type 1 diabetes mellitus") == ["E10-E14", "E11"]


def test_implication_needs_three_quarters_of_the_entries():
    entries = [CodeEntry("https://example.test/snomed/1", "4015004", "SNOMED CT", ("Chromosomes, group A (cell structure)",)),
               CodeEntry("https://example.test/snomed/2", "90152004", "SNOMED CT", ("Chromosome group (cell structure)",)),
               CodeEntry("https://example.test/snomed/3", "90152005", "SNOMED CT", ("Blood group (substance)",))]
    assert TerminologyIndex(entries).candidates("Group patients by encounters") == []  # 2 of 3 entries
    assert [e.code for e in TerminologyIndex(entries[:2]).candidates("Group patients by encounters")] == ["4015004", "90152004"]


def test_implication_needs_two_independent_entries():
    """A word seen in one entry implies nothing, and subcodes repeating their parent's label are not
    evidence: "kinds" must not find the German "Kind" of one scoliosis family."""
    index = TerminologyIndex([
        CodeEntry("https://example.test/snomed/224778002", "224778002", "SNOMED CT", ("Least restrictive environment (environment)",)),
        CodeEntry("https://example.test/icd/M41.0", "M41.0", "ICD-10-GM", ("Idiopathische Skoliose beim Kind",)),
        CodeEntry("https://example.test/icd/M41.00", "M41.00", "ICD-10-GM", ("Idiopathische Skoliose beim Kind: Mehrere Lokalisationen",)),
        CodeEntry("https://example.test/icd/M41.01", "M41.01", "ICD-10-GM", ("Idiopathische Skoliose beim Kind: Okzipito-Atlanto-Axialbereich",)),
        CodeEntry("https://example.test/icd/M41.02", "M41.02", "ICD-10-GM", ("Idiopathische Skoliose beim Kind: Zervikalbereich",)),
        CodeEntry("https://example.test/chop/00.0", "00.0", "CHOP", ("Therapeutischer Ultraschall", "Therapeutic ultrasound")),
    ])
    assert [e.code for e in index.candidates("Which kinds of therapeutic ultrasound do we bill most?")] == ["00.0"]
    assert index.candidates("Patients with at least one stay") == []
    assert index.implied("kind") == {"kind"} and index.implied("least") == {"least"}
    found = [e.code for e in index.candidates("Idiopathische Skoliose beim Kind")]
    assert found[0] == "M41.0" and set(found) == {"M41.0", "M41.00", "M41.01", "M41.02"}  # the family, by name


def test_english_alias_offers_therapeutic_ultrasound(tmp_path):
    index = TerminologyIndex([
        CodeEntry("https://example.test/snomed/262202000", "262202000", "SNOMED CT", ("Therapeutic intent (qualifier value)",)),
        CodeEntry("https://example.test/chop/00.0", "00.0", "CHOP", ("Therapeutischer Ultraschall", "Ultrason thérapeutique",
                                                                        *ENGLISH_ALIASES[("CHOP", "00.0")])),
    ])
    assert [e.code for e in index.candidates("For each year, how many therapeutic ultrasound procedures were billed?")] == ["00.0"]
    assert [e.code for e in index.candidates("Which kinds of therapeutic ultrasound do we bill most?")] == ["00.0"]
    labels = english_labels(tmp_path / "missing.jsonl")
    assert "Therapeutic ultrasound" in labels[("CHOP", "00.0")] and "Cholera" in labels[("ICD-10-GM", "A00")]
    assert {code for _, code in labels} >= {"00.0", "A00", "A00.0", "A00.1", "A00.9", "E11"}
    local = tmp_path / "english.jsonl"
    local.write_text('{"system":"CHOP","code":"00.0","labels":["Ultrasound therapy"]}\n'
                     '{"system":"ICD-10-GM","code":"A01","labels":["Typhoid fever"]}\n', encoding="utf-8")
    merged = english_labels(local)
    assert merged[("CHOP", "00.0")] >= {"Therapeutic ultrasound", "Ultrasound therapy"} and merged[("ICD-10-GM", "A01")] == {"Typhoid fever"}


def test_subcodes_follow_a_matched_code_without_displacing_direct_matches():
    index = TerminologyIndex([
        CodeEntry("https://example.test/icd/A00", "A00", "ICD-10-GM", ("Cholera",)),
        CodeEntry("https://example.test/icd/A00.0", "A00.0", "ICD-10-GM", ("Cholera durch Vibrio cholerae O:1, Biovar cholerae",)),
        CodeEntry("https://example.test/icd/A00.1", "A00.1", "ICD-10-GM", ("Cholera durch Vibrio cholerae O:1, Biovar eltor",)),
        CodeEntry("https://example.test/icd/A00.9", "A00.9", "ICD-10-GM", ("Cholera, nicht näher bezeichnet",)),
        CodeEntry("https://example.test/icd/A01", "A01", "ICD-10-GM", ("Typhus abdominalis und Paratyphus",)),
        CodeEntry("https://example.test/icd/E11", "E11", "ICD-10-GM", ("Diabetes mellitus, Typ 2",)),
        CodeEntry("https://example.test/icd/E11.0", "E11.0", "ICD-10-GM", ("Diabetes mellitus, Typ 2: Mit Koma",)),
        CodeEntry("https://example.test/snomed/77176002", "77176002", "SNOMED CT", ("Smoker (finding)",)),
    ])
    codes = lambda text, k=5: [entry.code for entry in index.candidates(text, k)]  # noqa: E731
    assert codes("How many patients have cholera?") == ["A00", "A00.0", "A00.1", "A00.9"]
    assert codes("How many patients have cholera?", k=2) == ["A00", "A00.0"]
    assert codes("Smokers with cholera or E11", k=3) == ["E11", "A00", "77176002"]  # direct matches first
    assert codes("Smokers with cholera or E11", k=5) == ["E11", "A00", "77176002", "E11.0", "A00.0"]
    assert index.children["A00"] == ["A00.0", "A00.1", "A00.9"] and "A00.0" not in index.children


@pytest.mark.parametrize("code,parent", [
    ("A00.0", "A00"), ("E11.01", "E11.0"), ("00.0", "00"), ("00.99.A3", "00.99.A"), ("A00", None), ("A00-A09", None), ("248153007", None),
])
def test_parent_code(code, parent):
    assert parent_code(code) == parent
