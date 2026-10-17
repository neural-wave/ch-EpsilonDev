import json

import pytest

from conftest import DATA_DIR, load_examples
from nl2sparql.context import SchemaCards, TerminologyIndex
from nl2sparql.judge_checks import coverage
from nl2sparql.judge_checks.coverage import CHECKS, check_question_coverage, check_record_count_asked
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


# ------------------------------------------------------------------------------- the record count
PRESCRIPTIONS = "How many drug prescriptions are for continuous dosing, and how many for intermittent dosing? Give the number of patients for each as well."
BY_PATTERN = (
    "SELECT ?time_pattern %s WHERE { GRAPH ?g { ?prescription a sphn:DrugPrescription ; sphn:hasTimePattern ?pattern . "
    "?pattern sphn:hasTypeCode ?code . ?code a ?concept . %s } "
    'VALUES (?concept ?time_pattern) { (snomed:255238004 "Continuous") (snomed:7087005 "Intermittent") } } GROUP BY ?time_pattern'
)
PATIENTS_ONLY = "(COUNT(DISTINCT ?g) AS ?patients)"
RECORD_HINT = (
    "The question asks how many drug prescriptions, but the query counts only patients. "
    "Add (COUNT(DISTINCT ?prescription) AS ?drug_prescriptions) next to (COUNT(DISTINCT ?g) AS ?patients)."
)


def record_hints(question: str, query: str, schema) -> list[str]:
    text = add_missing_prefixes(query.strip() + "\n")
    parsed = parse_sparql(text)
    assert parsed.algebra is not None, parsed.error
    return check_record_count_asked(question, parsed, text, schema)


def test_patients_only_breakdown_gets_the_record_count_with_the_query_s_own_variable(schema):
    assert record_hints(PRESCRIPTIONS, BY_PATTERN % (PATIENTS_ONLY, ""), schema) == [RECORD_HINT]


@pytest.mark.parametrize("select,pattern", [
    ("(COUNT(DISTINCT ?prescription) AS ?prescriptions) (COUNT(DISTINCT ?g) AS ?patients)", ""),  # the typed record
    ("(COUNT(?prescription) AS ?prescriptions)", ""),
    ("(COUNT(*) AS ?prescriptions) (COUNT(DISTINCT ?g) AS ?patients)", ""),
    ("(COUNT(DISTINCT ?code) AS ?prescriptions) (COUNT(DISTINCT ?g) AS ?patients)", ""),  # a code node: one per record
    ("(COUNT(DISTINCT ?pattern) AS ?prescriptions)", ""),  # a sub-node linked from the record
    ("(COUNT(DISTINCT ?dosed) AS ?prescriptions)", "BIND(?prescription AS ?dosed)"),  # an alias of the record
    ("(COUNT(DISTINCT ?matched) AS ?prescriptions)", "BIND(?prescription AS ?dosed) BIND(?dosed AS ?matched)"),
    ("(COUNT(DISTINCT ?drug) AS ?prescriptions)", "?prescription sphn:hasDrug ?drug ."),  # a linked node, untyped
    ("(COUNT(DISTINCT ?rx) AS ?prescriptions)", "?rx sphn:hasDrug ?drug . ?rx a sphn:DrugPrescription ."),
])
def test_a_query_that_counts_the_records_in_any_way_is_clean(select, pattern, schema):
    assert record_hints(PRESCRIPTIONS, BY_PATTERN % (select, pattern), schema) == []


def test_an_untyped_subject_of_an_sphn_property_is_a_record(schema):
    # q-041 counted ?record, typed nowhere: it is the subject of hasResult and hasMethodCode, so it is a record.
    query = (
        "SELECT ?method (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?record) AS ?records) WHERE { GRAPH ?g { "
        "?record sphn:hasResult ?weight . ?weight a sphn:BodyWeight . ?record sphn:hasMethodCode ?code . ?code a ?concept . } "
        'VALUES (?concept ?method) { (snomed:258104002 "Measured") } } GROUP BY ?method'
    )
    question = "How were the recorded body weights obtained? Give the number of weight records and of patients for each."
    assert record_hints(question, query, schema) == []


def test_a_per_patient_count_in_a_sub_select_answers_how_many_a_patient_has(schema):
    question = "By sex, how many prescriptions does a patient have on average and at most?"
    query = (
        "SELECT (AVG(?n) AS ?avg_prescriptions) (MAX(?n) AS ?max_prescriptions) WHERE { "
        "{ SELECT ?g (COUNT(DISTINCT ?prescription) AS ?n) WHERE { GRAPH ?g { ?prescription a sphn:DrugPrescription } } GROUP BY ?g } }"
    )
    assert record_hints(question, query, schema) == []


@pytest.mark.parametrize("select,pattern", [
    ("(COUNT(DISTINCT ?dosed) AS ?prescriptions) (COUNT(DISTINCT ?g) AS ?patients)", "BIND(?g AS ?dosed)"),  # a patient alias
    ("(COUNT(DISTINCT ?pid) AS ?prescriptions) (COUNT(DISTINCT ?g) AS ?patients)",
     "?prescription sphn:hasSubjectPseudoIdentifier ?pid ."),  # the patient's shared node
    ("(COUNT(DISTINCT ?concept) AS ?prescriptions) (COUNT(DISTINCT ?g) AS ?patients)", ""),  # kinds, not records
    ("(COUNT(DISTINCT ?g) AS ?patients) (SUM(1) AS ?prescriptions)", ""),
])
def test_patient_aliases_shared_nodes_and_kinds_are_not_record_counts(select, pattern, schema):
    assert record_hints(PRESCRIPTIONS, BY_PATTERN % (select, pattern), schema) == [RECORD_HINT]


@pytest.mark.parametrize("question,phrase", [
    ("How many imaging procedures were done for diagnosis, and how many patients had each kind?", "imaging procedures"),
    ("Give the number of stays and the average stay in days.", "stays"),
    ("Also give the number of follow-ups of each kind.", "follow-ups"),
    ("Also give the number of follow ups of each kind.", "follow-ups"),
    ("Give procedures and patients per side.", "procedures"),
    ("For each year, how many therapeutic ultrasound procedures were billed?", "therapeutic ultrasound procedures"),
    ("How many deaths were reported in each year?", "deaths"),
    ("How many recorded deaths were there?", "recorded deaths"),  # "recorded" is not the noun "record"
    ("How many such diagnoses were recorded?", "diagnoses"),  # a determiner is not repeated
    ("How many allergy episodes are confirmed? Also give the number of patients with at least one episode.", "allergy episodes"),
    ("For men and women: how many implant episodes were there?", "implant episodes"),
    ("Across billed procedures, how many procedures of each kind were done with curative intent?", "procedures"),
    ("How many lab tests has patient 0042 had?", "lab tests"),
    ("How many transplants and how many patients?", "transplants"),  # a common plural the class names do not spell
])
def test_the_cue_names_the_records_the_question_counts(question, phrase, schema):
    asked = coverage._asked_records(question, coverage._record_nouns(schema))
    assert asked is not None and asked.phrase == phrase


@pytest.mark.parametrize("question", [
    "How many patients have cholera on a hospital bill?",
    "How many of them have at least one prescription?",
    "How many of the recorded copy number variations are gains?",
    "How many pts had a bug identified in the lab?",
    "How many different procedures were billed?",  # a count of kinds
    "How many kinds of procedures were billed?",
    "How many billed and other medical procedures were done?",  # three modifiers
    "How many nationalities do our patients hold?",  # an attribute of the patient, not a record
    "How many procedure was done?",  # not a plural
    "How many were recorded?",
    "How many patients, procedures and days?",  # the clause ends at the comma
    "Which procedures were billed most often?",  # no cue
    "How many deaths were recorded?",  # a plural whose singular is not a class word without cards
])
def test_questions_that_count_patients_kinds_or_nothing_have_no_record_cue(question, schema):
    nouns = coverage._record_nouns(schema if "deaths" not in question else SchemaCards({}, []))
    assert coverage._asked_records(question, nouns) is None


def test_record_nouns_come_from_the_classes_with_a_pseudo_identifier(schema):
    nouns = coverage._record_nouns(schema)
    assert {"procedure", "prescription", "diagnosis", "episode", "event", "case", "encounter", "death", "follow-up"} <= nouns
    assert {"stay", "admission", "visit", "administration", "test", "transplant", "record"} <= nouns  # the common ones
    assert not {"drug", "country", "code", "gene", "nationality", "sex", "age"} & nouns
    assert coverage._record_nouns(SchemaCards({}, [])) == coverage.COMMON_RECORD_NOUNS


def test_the_hint_names_the_record_whose_class_the_question_names(schema):
    # Several typed variables: the class the question's words name wins, the backbone class does not.
    query = (
        "SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?s a sphn:AdministrativeSex ; sphn:hasCode ?sc . "
        "?sc a ?sex . ?proc a sphn:ImagingProcedure ; sphn:hasCode ?pc . } } GROUP BY ?sex"
    )
    assert record_hints("By sex, how many imaging procedures were done?", query, schema) == [
        "The question asks how many imaging procedures, but the query counts only patients. "
        "Add (COUNT(DISTINCT ?proc) AS ?imaging_procedures) next to (COUNT(DISTINCT ?g) AS ?patients)."
    ]
    # "stays" names AdministrativeCase through the card picker's synonyms; "deaths" picks Death over DeathDate.
    stays = (
        "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?s a sphn:AdministrativeSex . "
        "?case a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?adm . } }"
    )
    assert "Add (COUNT(DISTINCT ?case) AS ?stays)" in record_hints("Give the number of stays and patients.", stays, schema)[0]
    deaths = (
        "SELECT ?year (COUNT(DISTINCT ?g) AS ?deaths) WHERE { GRAPH ?g { ?death a sphn:Death ; sphn:hasDate ?death_date . "
        "?death_date a sphn:DeathDate ; sphn:hasYear ?year . } } GROUP BY ?year"
    )
    assert record_hints("How many deaths were reported in each year?", deaths, schema) == [
        "The question asks how many deaths, but the query counts only patients. "
        "Add (COUNT(DISTINCT ?death) AS ?deaths) next to (COUNT(DISTINCT ?g) AS ?patients)."  # ?deaths is taken
    ]


def test_the_hint_says_which_variables_may_be_the_record_when_several_tie(schema):
    query = (
        "SELECT ?intent (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "{ ?billed a sphn:BilledProcedure ; sphn:hasIntent ?i } UNION { ?medical a sphn:MedicalProcedure ; sphn:hasIntent ?i } "
        "UNION { ?imaging a sphn:ImagingProcedure ; sphn:hasIntent ?i } ?i sphn:hasCode ?c . ?c a ?intent . } } GROUP BY ?intent"
    )
    question = "Across billed, medical and imaging procedures, how many procedures of each kind were done with each intent?"
    assert record_hints(question, query, schema) == [
        "The question asks how many procedures, but the query counts only patients. "
        "Add (COUNT(DISTINCT ?record) AS ?procedures) next to (COUNT(DISTINCT ?g) AS ?patients) "
        "where ?record is the variable typed with the records' class (one of ?billed, ?medical, ?imaging)."
    ]


def test_a_projected_variable_nothing_binds_is_replaced_by_the_count(schema):
    # q-003's retry in three runs: SELECT ?stays ... GROUP BY ?stays, with nothing binding ?stays.
    query = (
        "SELECT ?stays (ROUND(AVG(?days)) AS ?avg_days) (MAX(?days) AS ?max_days) WHERE { GRAPH ?g { "
        "?case a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?admission ; sphn:hasDischargeDateTime ?discharge . } "
        'BIND (bif:datediff("day", ?admission, ?discharge) AS ?days) } GROUP BY ?stays'
    )
    question = "How long do hospital stays last? Give the number of stays and the average and longest stay in days."
    assert record_hints(question, query, schema) == [
        "The question asks how many stays, but nothing binds ?stays: replace it in the SELECT and GROUP BY with "
        "(COUNT(DISTINCT ?case) AS ?stays)."
    ]


def test_a_count_over_a_variable_nothing_binds_gets_the_record_bound_to_it(schema):
    # q-011: the record counts read ?telephone_followup, which no pattern binds; q-068 misnamed the record.
    query = (
        "SELECT (COUNT(DISTINCT ?g_telephone) AS ?patients_telephone) (COUNT(DISTINCT ?telephone_followup) AS ?telephone_followups) "
        "(COUNT(DISTINCT ?consultation_followup) AS ?consultation_followups) WHERE { "
        "{ GRAPH ?g_telephone { ?record a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:386473003 . } } UNION "
        "{ GRAPH ?g_consultation { ?record a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:281036007 . } } }"
    )
    question = "How many patients were followed up by telephone? Also give the number of follow-ups of each kind."
    assert record_hints(question, query, schema) == [
        "The question asks how many follow-ups, but nothing binds ?telephone_followup or ?consultation_followup, so the "
        "COUNT over it is empty. Bind the record, not the patient, inside the block that selects it: "
        "BIND(?record AS ?telephone_followup), and keep COUNT(DISTINCT ?telephone_followup)."
    ]
    misnamed = (
        "SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?prescription) AS ?prescriptions) WHERE { GRAPH ?g { "
        "?drug_prescription a sphn:DrugPrescription . } }"
    )
    assert record_hints("How many patients and the number of prescriptions?", misnamed, schema) == [
        "The question asks how many prescriptions, but nothing binds ?prescription, so the COUNT over it is empty. "
        "Bind the record, not the patient, inside the block that selects it: BIND(?drug_prescription AS ?prescription), "
        "and keep COUNT(DISTINCT ?prescription)."
    ]


def test_a_query_without_any_count_is_told_to_add_one(schema):
    query = (
        "SELECT (AVG(?days) AS ?avg_days) WHERE { GRAPH ?g { ?case a sphn:AdministrativeCase ; "
        "sphn:hasAdmissionDateTime ?admission ; sphn:hasDischargeDateTime ?discharge . } "
        'BIND (bif:datediff("day", ?admission, ?discharge) AS ?days) }'
    )
    assert record_hints("Give the number of stays and the average stay in days.", query, schema) == [
        "The question asks how many stays, but the query never counts them. Add (COUNT(DISTINCT ?case) AS ?stays) to the SELECT."
    ]
    untyped = "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?s ?p ?o } }"
    assert record_hints("How many stays and how many patients?", untyped, schema) == [
        "The question asks how many stays, but the query counts only patients. Add (COUNT(DISTINCT ?record) AS ?stays) "
        "next to (COUNT(DISTINCT ?g) AS ?patients) where ?record is the variable typed with the records' class."
    ]


def test_record_count_is_silent_without_a_question_a_cue_or_a_query_to_read(schema):
    query = add_missing_prefixes(BY_PATTERN % (PATIENTS_ONLY, ""))
    parsed = parse_sparql(query)
    assert check_record_count_asked("", parsed, query, schema) == []
    assert check_record_count_asked("How many patients have a prescription?", parsed, query, schema) == []
    update = "INSERT DATA { GRAPH <http://example.org/g> { <http://example.org/s> a <http://example.org/C> } }"
    assert check_record_count_asked(PRESCRIPTIONS, parse_sparql(update), update, schema) == []
    # Without cards only the common nouns are known: "prescriptions" is one of the cards' words.
    assert check_record_count_asked(PRESCRIPTIONS, parsed, query, SchemaCards({}, [])) == []
    # Without cards a code node still counts as a record (one per record); a node only linked to does not.
    codes = add_missing_prefixes(BY_PATTERN % ("(COUNT(DISTINCT ?code) AS ?stays)", ""))
    assert check_record_count_asked("How many stays?", parse_sparql(codes), codes, SchemaCards({}, [])) == []
    linked = add_missing_prefixes(BY_PATTERN % ("(COUNT(DISTINCT ?drug) AS ?stays)", "?prescription sphn:hasDrug ?drug ."))
    assert len(check_record_count_asked("How many stays?", parse_sparql(linked), linked, SchemaCards({}, []))) == 1
    assert check_record_count_asked("How many stays?", parsed, query, SchemaCards({}, [])) == [
        "The question asks how many stays, but the query counts only patients. Add (COUNT(DISTINCT ?prescription) AS ?stays) "
        "next to (COUNT(DISTINCT ?g) AS ?patients)."
    ]


def test_the_judge_can_loop_over_both_question_checks(index, schema):
    assert CHECKS == [check_question_coverage, check_record_count_asked]
    question = "How many cholera diagnoses are on hospital bills, and how many patients?"
    query = add_missing_prefixes(BY_SEX % "")
    parsed = parse_sparql(query)
    errors = [error for check in CHECKS for error in check(question, parsed, query, schema, index)]
    assert [error.split(",")[0] for error in errors] == [
        "The question asks about cholera (icd-10-gm:A00)",
        "The question asks how many cholera diagnoses",
    ]


REFERENCE_CUES = {  # the reference questions whose cue asks for a record count, as measured on 17 October
    "official-11", "team-ex-01", "q-003", "q-005", "q-011", "q-015", "q-027", "q-032", "q-035", "q-037", "q-039", "q-041",
    "q-042", "q-049", "q-052", "q-054", "q-055", "q-057", "q-059", "q-062", "q-064", "q-066", "q-068", "q-069", "q-074",
}


@pytest.mark.parametrize("case_id,question,query", reference_queries(), ids=[row[0] for row in reference_queries()])
def test_reference_queries_count_the_records_their_questions_ask_for(case_id, question, query, schema):
    asked = coverage._asked_records(question, coverage._record_nouns(schema))
    assert (asked is not None) == (case_id in REFERENCE_CUES), case_id
    text = add_missing_prefixes(query.strip() + "\n")
    parsed = parse_sparql(text)
    if parsed.algebra is None:
        return
    assert check_record_count_asked(question, parsed, text, schema) == [], case_id
