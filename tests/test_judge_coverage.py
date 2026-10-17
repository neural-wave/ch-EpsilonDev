import json

import pytest

from conftest import DATA_DIR, load_examples
from nl2sparql.context import SchemaCards, TerminologyIndex
from nl2sparql.judge_checks.coverage import check_question_coverage
from nl2sparql.models import CodeEntry
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import STANDARD_PREFIXES, add_missing_prefixes

ROOT = "https://biomedit.ch/rdf/sphn-resource/global"
OPTION = f'OPTION (inference "{ROOT}")'
ICD, CHOP, SNOMED = STANDARD_PREFIXES["icd-10-gm"], STANDARD_PREFIXES["chop"], STANDARD_PREFIXES["snomed"]
CHOLERA_BY_SEX = "How many patients have cholera on a hospital bill, by sex?"
BY_SEX = (
    "SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { %s"
    "?r a sphn:AdministrativeSex ; sphn:hasCode ?c . ?c a ?concept . } "
    'VALUES (?concept ?sex) { (snomed:248153007 "male") (snomed:248152002 "female") } } GROUP BY ?sex'
)
BILLED = "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . %s } }"
PROCEDURES = "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?procedure a sphn:BilledProcedure ; sphn:hasCode ?code . %s } }"


def entry(system: str, code: str, *labels: str) -> CodeEntry:
    namespace = {"ICD-10-GM": ICD, "CHOP": CHOP, "SNOMED CT": SNOMED}[system]
    return CodeEntry(namespace + code, code, system, labels)


@pytest.fixture(scope="module")
def index() -> TerminologyIndex:
    """A few codes with the English aliases scripts/build_terminology_index.py adds; never the real index."""
    return TerminologyIndex([
        entry("CHOP", "00.0", "Therapeutic ultrasound"),
        entry("ICD-10-GM", "A00", "Cholera", "Choléra"),
        entry("ICD-10-GM", "A00.0", "Classical cholera"),
        entry("ICD-10-GM", "A00.1", "El Tor cholera"),
        entry("ICD-10-GM", "A00.9", "Cholera, unspecified"),
        entry("ICD-10-GM", "E10-E14", "Diabetes mellitus"),
        entry("ICD-10-GM", "E11", "Non-insulin-dependent diabetes mellitus", "Type 2 diabetes mellitus"),
        entry("ICD-10-GM", "O30.1", "Triplés"),  # one French word that happens to be English
        entry("ICD-10-GM", "C80.9", "Malignant tumour"),  # names sphn:OncologyDiagnosis (synonym "tumour")
        entry("ICD-10-GM", "T88.7", "Adverse event"),  # names sphn:AdverseEvent (both parts of the name)
        entry("SNOMED CT", "32570681000036106", "Indeterminate sex (finding)"),
        entry("SNOMED CT", "63650001", "Cholera (disorder)"),
    ])


@pytest.fixture(scope="module")
def schema() -> SchemaCards:
    return SchemaCards.load(DATA_DIR / "schema_cards.json")


def hints(question: str, query: str, index, schema) -> list[str]:
    """Run the check the way the judge does: prefixes added, parsed, text and algebra passed along."""
    text = add_missing_prefixes(query.strip() + "\n")
    parsed = parse_sparql(text)
    assert parsed.algebra is not None, parsed.error
    return check_question_coverage(question, parsed, text, schema, index)


def hint(name: str, concept: str, record: str = "dx") -> str:
    return (
        f"The question asks about {name} ({concept}), but the query never restricts to it. Add it to the record "
        f"the question is about: ?{record} sphn:hasCode ?code . ?code a {concept} {OPTION} ."
    )


def reference_queries() -> list[tuple[str, str, str]]:
    """(id, question, query) of the official and team examples and the dev reference queries; holdout rows
    are a blind exam and stay closed."""
    rows = [(row["id"], row["question"], row["sparql"]) for row in load_examples()]
    for line in (DATA_DIR / "team_examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows.append((row["id"], row["question"], row["sparql"]))
    for line in (DATA_DIR.parent / "eval" / "query_questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("split") == "dev":
                rows.append((row["id"], row["question"], row["reference_sparql"]))
    return rows


# ------------------------------------------------------------------------------------- the console case
def test_query_that_dropped_the_concept_gets_the_pattern_to_add(index, schema):
    assert hints(CHOLERA_BY_SEX, BY_SEX % "", index, schema) == [hint("cholera", "icd-10-gm:A00")]


def test_query_that_keeps_the_concept_is_clean(index, schema):
    pattern = f"?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a icd-10-gm:A00 {OPTION} . "
    assert hints(CHOLERA_BY_SEX, BY_SEX % pattern, index, schema) == []


@pytest.mark.parametrize("restriction", [
    f"?code a icd-10-gm:A00.1 {OPTION} .",  # a subcode
    f"?code a ?concept {OPTION} . VALUES ?concept {{ icd-10-gm:A00.0 icd-10-gm:A00.9 }}",
    "?code a ?concept . FILTER (?concept IN (icd-10-gm:A00, icd-10-gm:B01))",
    "?code a <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/2022/A00.1> .",  # the data's year-versioned class
    'FILTER (STRSTARTS(STR(?code), "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/A00"))',
    'FILTER (CONTAINS(STR(?code), "ICD-10-GM-2022-A00.1"))',  # the code node IRI spells the code
    'GRAPH <https://biomedit.ch/rdf/sphn-resource/global> { ?concept rdfs:label ?label } '
    'FILTER (CONTAINS(LCASE(?label), "cholera"))',  # the concept by its name
    "?code a snomed:63650001 .",  # the same concept under another terminology
])
def test_every_way_of_naming_the_code_or_a_relative_covers_it(restriction, index, schema):
    assert hints("How many patients have cholera?", BILLED % restriction, index, schema) == []


def test_a_parent_code_covers_the_subcode_the_question_names(index, schema):
    question = "How many patients have classical cholera?"
    assert hints(question, BILLED % f"?code a icd-10-gm:A00 {OPTION} .", index, schema) == []
    assert hints(question, BILLED % "", index, schema) == [hint("classical cholera", "icd-10-gm:A00.0")]


def test_chop_codes_follow_their_own_hierarchy(index, schema):
    question = "How many patients had a therapeutic ultrasound?"
    assert hints(question, PROCEDURES % f"?code a chop:00.01 {OPTION} .", index, schema) == []
    regex = '?code a ?concept . FILTER (REGEX(REPLACE(STR(?concept), "^.*/", ""), "^00\\\\.0[0-9]$"))'
    assert hints(question, PROCEDURES % regex, index, schema) == []
    expected = [hint("therapeutic ultrasound", "chop:00.0", "procedure")]
    assert hints(question, PROCEDURES % f"?code a chop:00.1 {OPTION} .", index, schema) == expected  # a sibling
    # A number or a time that contains the digits is not the code; a wrong namespace is not the concept.
    noise = ('?procedure sphn:hasStartDateTime ?start . BIND (1000.0 * 2 AS ?x) '
             'FILTER (STR(?start) != "2020-01-01T00:00:00" && STRSTARTS(STR(?code), "http://chop.org/id/"))')
    assert hints(question, PROCEDURES % noise, index, schema) == expected


def test_the_most_specific_code_of_a_family_is_named_once(index, schema):
    question = "How many patients have a Billed Diagnosis for Type 2 diabetes mellitus (non-insulin-dependent diabetes mellitus)?"
    assert hints(question, BILLED % "", index, schema) == [hint("Type 2 diabetes mellitus", "icd-10-gm:E11")]
    # The block above it covers it, and so does E11 itself.
    assert hints(question, BILLED % f"?code a icd-10-gm:E10-E14 {OPTION} .", index, schema) == []
    assert hints(question, BILLED % f"?code a icd-10-gm:E11 {OPTION} .", index, schema) == []


def test_a_string_that_spells_a_code_inside_or_around_an_icd_block_covers_it(index, schema):
    block = "How many patients have diabetes mellitus?"
    inside = 'FILTER (STRSTARTS(STR(?concept), "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/E11")) ?code a ?concept .'
    assert hints(block, BILLED % inside, index, schema) == []
    assert hints(block, BILLED % "", index, schema) == [hint("diabetes mellitus", "icd-10-gm:E10-E14")]
    around = '?code a ?concept . FILTER (CONTAINS(STR(?concept), "E10-E14"))'
    assert hints("How many patients have type 2 diabetes mellitus?", BILLED % around, index, schema) == []


def test_an_icd_code_written_in_the_question_is_required(index, schema):
    assert hints("How many patients have E11 on a bill?", BILLED % "", index, schema) == [hint("E11", "icd-10-gm:E11")]


def test_hints_follow_the_question_and_name_the_label_when_other_words_stand_between(index, schema):
    question = "Of the patients with a therapeutic ultrasound, how many have cholera of unspecified type?"
    assert hints(question, PROCEDURES % "", index, schema) == [
        hint("therapeutic ultrasound", "chop:00.0", "procedure"),
        hint("Cholera, unspecified", "icd-10-gm:A00.9"),
    ]


# ------------------------------------------------------------------------------- what is not strong enough
@pytest.mark.parametrize("question", [
    "How many triples are stored in the database?",  # a one-word label that only one language has
    "How many patients have diabetes?",  # one word that implies "Diabetes mellitus": a prompt candidate only
    "Which therapeutic procedures used ultrasound?",  # the label's words are not side by side
    "How many patients are of indeterminate sex?",  # SNOMED CT categories are left to the query
    "How many patients have a malignant tumour?",  # the words name sphn:OncologyDiagnosis
    "How many patients had an adverse event?",  # the words name sphn:AdverseEvent
])
def test_weak_matches_and_concepts_a_class_covers_stay_silent(question, index, schema):
    assert hints(question, BILLED % "", index, schema) == []


def test_the_implication_rule_feeds_the_prompt_but_not_the_check(index, schema):
    assert "E10-E14" in [candidate.code for candidate in index.candidates("How many patients have diabetes?")]
    assert hints("How many patients have diabetes?", BILLED % "", index, schema) == []


def test_silent_without_a_terminology_a_question_or_a_query_to_read(index, schema):
    query = add_missing_prefixes(BY_SEX % "")
    parsed = parse_sparql(query)
    assert check_question_coverage(CHOLERA_BY_SEX, parsed, query, schema, None) == []
    assert check_question_coverage(CHOLERA_BY_SEX, parsed, query, schema, TerminologyIndex([])) == []
    assert check_question_coverage("", parsed, query, schema, index) == []
    update = "INSERT DATA { GRAPH <http://example.org/g> { <http://example.org/s> a <http://example.org/C> } }"
    assert check_question_coverage(CHOLERA_BY_SEX, parse_sparql(update), update, schema, index) == []


def test_without_schema_cards_the_class_exemption_is_off(index):
    no_cards = SchemaCards({}, [])
    question = "How many patients have a malignant tumour?"
    assert hints(question, BILLED % "", index, no_cards) == [hint("malignant tumour", "icd-10-gm:C80.9")]
    # SNOMED CT stays silent on its own, not only because "sex" names sphn:AdministrativeSex.
    assert hints("How many patients are of indeterminate sex?", BILLED % "", index, no_cards) == []


@pytest.mark.parametrize("case_id,question,query", reference_queries(), ids=[row[0] for row in reference_queries()])
def test_reference_queries_cover_what_their_questions_name(case_id, question, query, index, schema):
    text = add_missing_prefixes(query.strip() + "\n")
    parsed = parse_sparql(text)
    if parsed.algebra is None:
        return
    assert check_question_coverage(question, parsed, text, schema, index) == [], case_id
