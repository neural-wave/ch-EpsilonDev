import copy
import json
import re
from pathlib import Path

import pytest

from conftest import DATA_DIR, load_examples
from nl2sparql.context import SchemaCards, TerminologyIndex
from nl2sparql.judge import Judge
from nl2sparql.judge_checks.schema import (
    CHECKS,
    REWRITES,
    TERMINOLOGY_CHECKS,
    check_class_property,
    check_code_home,
    check_code_system,
    check_record_link_typed,
    check_typed_value_set,
    check_value_set_member,
    rewrite_typed_value_set,
    rewrite_year_versioned_code_key,
    suggest_missing_term,
)
from nl2sparql.models import CodeEntry
from nl2sparql.sparql.parse import parse_sparql
from nl2sparql.sparql.prefixes import add_missing_prefixes, fix_standard_prefixes

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GRAPH_COUNT = "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { %s } }"


@pytest.fixture(scope="module")
def schema() -> SchemaCards:
    return SchemaCards.load(DATA_DIR / "schema_cards.json", FIXTURES / "value_sets_small.json")


def run(check, query: str, schema: SchemaCards) -> list[str]:
    text = add_missing_prefixes(query.strip() + "\n")
    parsed = parse_sparql(text)
    assert parsed.algebra is not None, parsed.error
    return check(parsed, text, schema)


def cards(classes: dict) -> SchemaCards:
    return SchemaCards({name: {"instances": 1, "patients": 1, "properties": props} for name, props in classes.items()}, [])


def test_class_property_lists_the_properties_of_the_class(schema):
    query = GRAPH_COUNT % "?stay a sphn:AdministrativeCase ; sphn:hasStartDateTime ?start ; sphn:hasEndDateTime ?end ."
    assert run(check_class_property, query, schema) == [
        "sphn:AdministrativeCase has no sphn:hasStartDateTime and sphn:hasEndDateTime in the data. "
        "Its properties: sphn:hasAdmissionDateTime, sphn:hasCareHandling -> sphn:CareHandling, sphn:hasDischargeDateTime, "
        "sphn:hasDischargeLocation -> sphn:Location, sphn:hasIdentifier, sphn:hasOriginLocation -> sphn:Location."
    ]
    assert run(check_class_property, GRAPH_COUNT % "?drug a sphn:Drug ; sphn:hasName ?name .", schema) == [
        "sphn:Drug has no sphn:hasName in the data. Its properties: sphn:hasActiveIngredient -> sphn:Substance, "
        "sphn:hasArticle -> sphn:DrugArticle, sphn:hasInactiveIngredient -> sphn:Substance, sphn:hasQuantity -> sphn:Quantity."
    ]


def test_class_property_names_the_one_close_replacement():
    stems = cards({"Stay": {"hasRank": {"kind": "individual"}}, "Other": {"hasRankCode": {"kind": "resource"}}})
    assert run(check_class_property, GRAPH_COUNT % "?s a sphn:Stay ; sphn:hasRankCode ?r .", stems) == [
        "sphn:Stay has no sphn:hasRankCode in the data. Write sphn:hasRank instead of sphn:hasRankCode. Its properties: sphn:hasRank."
    ]
    close = cards({
        "Stay": {"hasAdmissionDateTime": {"kind": "literal"}, "hasDischargeDateTime": {"kind": "literal"}},
        "Other": {"hasAdmissionDate": {"kind": "literal"}},
    })
    assert run(check_class_property, GRAPH_COUNT % "?s a sphn:Stay ; sphn:hasAdmissionDate ?d .", close) == [
        "sphn:Stay has no sphn:hasAdmissionDate in the data. Write sphn:hasAdmissionDateTime instead of sphn:hasAdmissionDate. "
        "Its properties: sphn:hasAdmissionDateTime, sphn:hasDischargeDateTime."
    ]


def test_class_property_ignores_unknown_terms_and_other_typings(schema):
    # A property that occurs nowhere is the term check's business.
    assert run(check_class_property, GRAPH_COUNT % "?s a sphn:AdministrativeCase ; sphn:hasRankCode ?r .", schema) == []
    variable = "VALUES ?cls { sphn:AdministrativeCase } ?s a ?cls ; sphn:hasStartDateTime ?t ."
    assert run(check_class_property, GRAPH_COUNT % variable, schema) == []
    twice = "?s a sphn:AdministrativeCase, sphn:HealthcareEncounter ; sphn:hasStartDateTime ?t ."
    assert run(check_class_property, GRAPH_COUNT % twice, schema) == []
    # A concrete class decides even when the subject is also typed by a variable (q-076).
    concrete = "?drug a sphn:Drug ; sphn:hasName ?name . ?drug a ?drug_concept ."
    assert len(run(check_class_property, GRAPH_COUNT % concrete, schema)) == 1


def test_class_property_scopes_union_branches_and_sees_optional_and_exists(schema):
    branches = (
        "{ ?x a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?t } "
        "UNION { ?x a sphn:HealthcareEncounter ; sphn:hasStartDateTime ?t }"
    )
    assert run(check_class_property, GRAPH_COUNT % branches, schema) == []
    optional = "?x a sphn:AdministrativeCase . OPTIONAL { ?x sphn:hasStartDateTime ?t }"
    assert run(check_class_property, GRAPH_COUNT % optional, schema)[0].startswith("sphn:AdministrativeCase has no sphn:hasStartDateTime")
    exists = (
        "SELECT ?cohort (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?case a sphn:AdministrativeCase } "
        "BIND (IF(EXISTS { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasAdministrativeCase ?case ; sphn:hasStartDateTime ?s } }, "
        '"yes", "no") AS ?cohort) } GROUP BY ?cohort'
    )
    assert run(check_class_property, exists, schema)[0].startswith("sphn:BilledDiagnosis has no sphn:hasStartDateTime")


def test_record_link_typed_as_a_code_node(schema):
    query = (
        "SELECT ?specialty_concept (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "?encounter a sphn:HealthcareEncounter ; sphn:hasTherapeuticArea ?area_code . ?area_code a ?specialty_concept . } } "
        "GROUP BY ?specialty_concept"
    )
    assert run(check_record_link_typed, query, schema) == [
        "?area_code is a sphn:TherapeuticArea record (object of hasTherapeuticArea), not a code node: "
        "'?area_code a ?specialty_concept' only yields that class. "
        "Use its own properties: hasSpecialtyName (value set, group by it directly)."
    ]
    intent = GRAPH_COUNT % "?proc sphn:hasIntent ?intent_code . ?intent_code a ?intent_concept . FILTER (?intent_concept IN (snomed:373808002))"
    assert run(check_record_link_typed, intent, schema) == [
        "?intent_code is a sphn:Intent record (object of hasIntent), not a code node: "
        "'?intent_code a ?intent_concept' only yields that class. "
        "Use its own properties: hasCode (code node: ?r sphn:hasCode ?c . ?c a ?concept)."
    ]


def test_record_link_exempts_values_several_targets_and_code_links(schema):
    reference = next(example for example in load_examples() if example["id"] == "official-08")
    assert run(check_record_link_typed, reference["sparql"], schema) == []
    results = GRAPH_COUNT % "?test a sphn:LabTestEvent ; sphn:hasResult ?result . ?result a ?kind ."
    assert run(check_record_link_typed, results, schema) == []
    code = GRAPH_COUNT % "?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?concept ."
    assert run(check_record_link_typed, code, schema) == []
    listed = cards({
        "Descriptor": {"hasVariation": {"kind": "resource", "target": "Snv", "targets": ["Snv", "Deletion"]}},
        "Snv": {"hasAllele": {"kind": "literal"}}, "Deletion": {"hasAllele": {"kind": "literal"}},
    })
    assert run(check_record_link_typed, GRAPH_COUNT % "?d sphn:hasVariation ?v . ?v a ?cls .", listed) == []
    single = cards({"Descriptor": {"hasVariation": {"kind": "resource", "targets": ["Snv"]}}, "Snv": {"hasAllele": {"kind": "literal", "datatype": "string"}}})
    assert run(check_record_link_typed, GRAPH_COUNT % "?d sphn:hasVariation ?v . ?v a ?cls .", single) == [
        "?v is a sphn:Snv record (object of hasVariation), not a code node: '?v a ?cls' only yields that class. "
        "Use its own properties: hasAllele (string)."
    ]


Q044 = """PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
SELECT ?fixation_concept (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?specimen) AS ?specimens)
WHERE {
  GRAPH ?g {
    ?specimen a sphn:TumorSpecimen ;
              sphn:hasFixationType ?fixation_type .
    ?fixation_type a ?fixation_concept .
  }
}
GROUP BY ?fixation_concept
ORDER BY DESC(?patients)
LIMIT 5
"""


def test_rewrite_typed_value_set_drops_the_type_triple_and_renames(schema):
    rewritten, repairs = rewrite_typed_value_set(Q044, schema)
    assert repairs == [
        "Removed '?fixation_type a ?fixation_concept' and replaced ?fixation_concept by ?fixation_type: "
        "sphn:hasFixationType holds a value-set entry (sphn-individual:...), which is the category itself, not a code node."
    ]
    assert "?fixation_concept" not in rewritten and "?fixation_type a" not in rewritten
    assert "SELECT ?fixation_type" in rewritten and "GROUP BY ?fixation_type" in rewritten
    parsed = parse_sparql(rewritten)
    assert parsed.kind == "SelectQuery"
    assert check_typed_value_set(parsed, rewritten, schema) == []


@pytest.mark.parametrize("body, expected", [
    ("?s sphn:hasFixationType ?f . ?f a ?c ; rdfs:label ?l .", "?s sphn:hasFixationType ?f . ?f rdfs:label ?l ."),
    ("?s sphn:hasFixationType ?f . ?f rdfs:label ?l ; a ?c .", "?s sphn:hasFixationType ?f . ?f rdfs:label ?l ."),
    ("?s sphn:hasFixationType ?f . ?f rdf:type ?c", "?s sphn:hasFixationType ?f . "),
    ('?s sphn:hasFixationType ?f . ?f a ?c OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .', "?s sphn:hasFixationType ?f . "),
])
def test_rewrite_handles_chains_and_the_last_statement(schema, body, expected):
    query = add_missing_prefixes("SELECT ?f WHERE { GRAPH ?g { %s } }" % body)  # the judge declares prefixes first
    rewritten, repairs = rewrite_typed_value_set(query, schema)
    assert len(repairs) == 1
    assert rewritten == query.replace(body, expected)


def test_rewrite_leaves_the_query_when_it_cannot_do_it_cleanly(schema):
    both = "SELECT ?f ?c WHERE { GRAPH ?g { ?s sphn:hasFixationType ?f . ?f a ?c } }"
    assert rewrite_typed_value_set(both, schema) == (both, [])
    assert run(check_typed_value_set, both, schema) == [
        "sphn:hasFixationType holds a value-set entry (sphn-individual:...), not a code node: "
        "drop '?f a ...' and group by or compare ?f itself."
    ]
    code = "SELECT ?concept WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?concept } }"
    assert rewrite_typed_value_set(code, schema) == (code, [])
    assert run(check_typed_value_set, code, schema) == []
    assert rewrite_typed_value_set("SELECT ?x WHERE {", schema) == ("SELECT ?x WHERE {", [])


# official-06's answer in the 17 October runs (query-20261017-210737): ?concept is the year-versioned CHOP
# class (chop/2016/00.03, chop/2017/00.03, ...), so it returned 31 rows instead of the reference's 4 codes.
OFFICIAL_06_ANSWER = """DEFINE sql:select-option "order"
PREFIX sphn-individual: <https://biomedit.ch/rdf/sphn-schema/sphn/individual#>
PREFIX bif: <bif:>
PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
PREFIX chop: <https://biomedit.ch/rdf/sphn-resource/chop/>

SELECT ?concept
       (COUNT(DISTINCT ?procedure) AS ?count)
       (ROUND(1000.0 * COUNT(DISTINCT ?principal) / COUNT(DISTINCT ?procedure)) / 10 AS ?principal_pct)
       (ROUND(10 * AVG(?hours)) / 10 AS ?avg_hours)
WHERE {
  GRAPH ?g {
    ?code a chop:00.0 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .
    ?procedure sphn:hasCode ?code .
    ?code a ?concept .
    ?procedure a sphn:BilledProcedure ;
               sphn:hasStartDateTime ?start ;
               sphn:hasEndDateTime ?end .
    OPTIONAL {
      ?procedure sphn:hasRank sphn-individual:Principal .
      BIND (?procedure AS ?principal)
    }
  }
  BIND (bif:datediff("hour", ?start, ?end) AS ?hours)
}
GROUP BY ?concept
ORDER BY DESC(?count)
"""
YEAR_FREE_REPAIR = (
    "Grouped ?concept by the year-free code: '?code a ?concept' yields one ICD-10-GM or CHOP class per code and "
    "coding year (chop/2016/00.03, chop/2017/00.03), so the node is typed ?concept_versioned and ?concept is "
    'REPLACE(STR(?concept_versioned), "^.*/", "")'
)


def test_rewrite_year_versioned_code_key_merges_the_years_of_official_06(schema):
    rewritten, repairs = rewrite_year_versioned_code_key(OFFICIAL_06_ANSWER, schema)
    assert repairs == [YEAR_FREE_REPAIR + '; FILTER (?concept != "00.0") drops the code ?code is typed with.']
    assert rewritten == OFFICIAL_06_ANSWER.replace("?code a ?concept .", "?code a ?concept_versioned .").replace(
        "      BIND (?procedure AS ?principal)\n    }\n",
        "      BIND (?procedure AS ?principal)\n    }\n"
        '    BIND (REPLACE(STR(?concept_versioned), "^.*/", "") AS ?concept)\n'
        '    FILTER (?concept != "00.0")\n',
    )
    assert parse_sparql(rewritten).kind == "SelectQuery"
    assert rewrite_year_versioned_code_key(rewritten, schema) == (rewritten, [])  # ?concept is now the BIND's


def test_judge_repairs_official_06_without_new_errors(schema):
    verdict = Judge(schema).check(OFFICIAL_06_ANSWER)
    assert verdict.errors == []
    assert [repair for repair in verdict.repairs if repair.startswith("Grouped ?concept")] == [
        YEAR_FREE_REPAIR + '; FILTER (?concept != "00.0") drops the code ?code is typed with.'
    ]
    assert 'BIND (REPLACE(STR(?concept_versioned), "^.*/", "") AS ?concept)' in verdict.sparql


@pytest.mark.parametrize("body, expected", [
    # The record's card says ICD-10-GM; no concrete code, so no filter. The brace closes the line: inline.
    ("?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?concept . } }",
     '?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . ?code a ?concept_versioned . '
     'BIND (REPLACE(STR(?concept_versioned), "^.*/", "") AS ?concept) } }'),
    # An untyped record: the concrete ICD-10-GM code names the system and the parent to drop.
    ('?code a icd-10-gm:A00 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") . '
     "?dx sphn:hasCode ?code . ?code rdf:type ?concept } }",
     '?code a icd-10-gm:A00 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") . '
     '?dx sphn:hasCode ?code . ?code rdf:type ?concept_versioned BIND (REPLACE(STR(?concept_versioned), "^.*/", "") AS ?concept) '
     'FILTER (?concept != "A00") } }'),
])
def test_rewrite_year_versioned_code_key_reads_the_system_from_the_card_or_the_code(schema, body, expected):
    head = "SELECT ?concept (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
    query = add_missing_prefixes(head + body + " GROUP BY ?concept ORDER BY DESC(?patients) LIMIT 10")
    rewritten, repairs = rewrite_year_versioned_code_key(query, schema)
    assert len(repairs) == 1
    assert rewritten == query.replace(body, expected)
    assert parse_sparql(rewritten).kind == "SelectQuery"


@pytest.mark.parametrize("body", [
    # A label lookup needs the class IRI (official-06's answer in query-20261017-214008).
    "GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept . } "
    "OPTIONAL { GRAPH <https://biomedit.ch/rdf/sphn-resource/global> { ?concept rdfs:label ?label } }",
    "GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept . } VALUES ?concept { chop:00.01 chop:00.02 }",
    "GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept . } FILTER (STRSTARTS(STR(?concept), STR(chop:)))",
    "GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept . } BIND (STR(?concept) AS ?iri)",
    "GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . OPTIONAL { ?code a ?concept } }",
    "GRAPH ?g { { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept } UNION "
    "{ ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code } }",
    "GRAPH ?g { ?sex a sphn:AdministrativeSex ; sphn:hasCode ?code . ?code a ?concept . }",  # SNOMED CT
    "GRAPH ?g { ?dx a sphn:Diagnosis ; sphn:hasCode ?code . ?code a ?concept . }",  # generic sphn:Code nodes
    "GRAPH ?g { ?x sphn:hasCode ?code . ?code a ?concept . }",  # no class, no code: the system is unknown
    "GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept . ?other a ?concept_versioned . }",
])
def test_rewrite_year_versioned_code_key_gives_up(schema, body):
    query = add_missing_prefixes("SELECT ?concept (COUNT(DISTINCT ?g) AS ?patients) WHERE { %s } GROUP BY ?concept" % body)
    assert parse_sparql(query).kind == "SelectQuery"
    assert rewrite_year_versioned_code_key(query, schema) == (query, [])


def test_rewrite_year_versioned_code_key_needs_a_group_key_and_cards(schema):
    # The pattern every give-up case above changes in one place is rewritten as it stands.
    base = "GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept . }"
    query = add_missing_prefixes("SELECT ?concept (COUNT(DISTINCT ?g) AS ?patients) WHERE { %s } GROUP BY ?concept" % base)
    assert len(rewrite_year_versioned_code_key(query, schema)[1]) == 1
    counted = add_missing_prefixes(
        "SELECT (COUNT(DISTINCT ?concept) AS ?codes) WHERE { GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . ?code a ?concept . } }"
    )
    assert rewrite_year_versioned_code_key(counted, schema) == (counted, [])
    assert rewrite_year_versioned_code_key(OFFICIAL_06_ANSWER, SchemaCards({}, [])) == (OFFICIAL_06_ANSWER, [])
    assert rewrite_year_versioned_code_key("SELECT ?x WHERE {", schema) == ("SELECT ?x WHERE {", [])


def team_and_dev_references() -> list[tuple[str, str]]:
    """The team examples and the dev reference queries; holdout lines are a blind exam, skipped unparsed."""
    queries = []
    for line in (DATA_DIR / "team_examples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            queries.append((row["id"], row["sparql"]))
    for line in (DATA_DIR.parent / "eval" / "query_questions.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip() or re.search(r'"split"\s*:\s*"holdout"', line):
            continue
        row = json.loads(line)
        if row.get("split") == "dev":
            queries.append((row["id"], row["reference_sparql"]))
    return queries


@pytest.mark.parametrize("case_id,query", team_and_dev_references(), ids=[case[0] for case in team_and_dev_references()])
def test_no_schema_rewrite_touches_the_team_examples_and_dev_references(schema, case_id, query):
    text = add_missing_prefixes(fix_standard_prefixes(query.strip() + "\n"))
    for rewrite in REWRITES:
        assert rewrite(text, schema) == (text, []), (case_id, rewrite.__name__)


def test_value_set_member_on_the_wrong_class(schema):
    q039 = (
        "SELECT ?outcome_concept (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        "?follow_up a sphn:FollowUp ; sphn:hasCode ?code ; sphn:hasResult ?vital_status . } ?code a ?outcome_concept . "
        "FILTER (?outcome_concept = snomed:438949009 || ?outcome_concept = snomed:399307001) } GROUP BY ?outcome_concept"
    )
    assert run(check_value_set_member, q039, schema) == [
        "snomed:399307001 (Lost to follow-up) and snomed:438949009 (Alive) are not values of sphn:FollowUp hasCode "
        "(its values: Telephone follow-up snomed:386473003 | Follow-up consultation snomed:281036007). "
        "They are values of sphn:VitalStatus hasCode: write ?follow_up sphn:hasResult ?vital_status . "
        "?vital_status sphn:hasCode ?code . ?code a ?concept."
    ]
    values = GRAPH_COUNT % (
        "?follow_up a sphn:FollowUp ; sphn:hasCode ?fu_code . ?fu_code a ?fu_concept . "
        'VALUES (?fu_concept ?group_name) { (snomed:438949009 "Alive") }'
    )
    assert run(check_value_set_member, values, schema)[0].startswith("snomed:438949009 (Alive) is not a value of sphn:FollowUp hasCode")
    typed = GRAPH_COUNT % "?follow_up a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:438949009 ."
    assert len(run(check_value_set_member, typed, schema)) == 1
    rank = GRAPH_COUNT % "?proc a sphn:BilledProcedure ; sphn:hasRank sphn-individual:Secondary ."
    assert run(check_value_set_member, rank, schema) == [
        "sphn-individual:Secondary (Secondary) is not a value of sphn:BilledProcedure hasRank "
        "(its values: Principal sphn-individual:Principal | Supplementary sphn-individual:Supplementary). "
        "It is a value of sphn:BilledDiagnosis hasRank: write ?proc a sphn:BilledDiagnosis ; sphn:hasRank sphn-individual:Secondary."
    ]


def test_value_set_member_of_another_property_of_the_same_class_gets_no_rewrite(schema):
    # official-09: "write ?event sphn:hasConsequences sphn-individual:Death" was copied in 3 of 3 runs and
    # changed the cohort from serious events to fatal ones. The hint now lists the values and stops.
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?events) (COUNT(DISTINCT ?death_patient) AS ?deaths) WHERE { GRAPH ?g { "
        "?event a sphn:AdverseEvent ; sphn:hasConsequences ?consequence . } OPTIONAL { GRAPH ?g { "
        "?event sphn:hasOutcome sphn-individual:Death . BIND (?g AS ?death_patient) } } }"
    )
    assert run(check_value_set_member, query, schema) == [
        "sphn-individual:Death (Death) is not a value of sphn:AdverseEvent hasOutcome; its values: Recovering | Recovered | "
        "NotRecovered | Fatal | RecoveringWithSequelae: use the one the question means (Death is a value of hasConsequences)."
    ]
    assert run(check_value_set_member, query.replace("sphn-individual:Death", "sphn-individual:Fatal"), schema) == []


def test_value_set_member_stays_silent_without_evidence(schema):
    own = GRAPH_COUNT % "?follow_up a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:386473003 ."
    assert run(check_value_set_member, own, schema) == []
    unknown = GRAPH_COUNT % "?follow_up a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:1 ."
    assert run(check_value_set_member, unknown, schema) == []
    untyped = GRAPH_COUNT % "?follow_up sphn:hasCode ?code . ?code a snomed:438949009 ."
    assert run(check_value_set_member, untyped, schema) == []
    without_sets = SchemaCards.load(DATA_DIR / "schema_cards.json")
    typed = GRAPH_COUNT % "?follow_up a sphn:FollowUp ; sphn:hasCode ?code . ?code a snomed:438949009 ."
    assert run(check_value_set_member, typed, without_sets) == []


@pytest.fixture(scope="module")
def coded(schema) -> SchemaCards:
    """The cards with the per-property 'code_system' field another agent is adding."""
    classes = copy.deepcopy(schema.classes)
    systems = {"BilledProcedure": "CHOP", "Diagnosis": "generic", "BilledDiagnosis": "ICD-10-GM",
               "OncologyDiagnosis": "ICD-10-GM", "RadiotherapyProcedure": "SNOMED CT"}
    for name, system in systems.items():
        classes[name]["properties"]["hasCode"]["code_system"] = system
    return SchemaCards(classes, list(schema.schema_terms), schema.value_sets)


def test_code_system_against_the_class(coded):
    chop = GRAPH_COUNT % '?t a sphn:BilledProcedure ; sphn:hasCode ?tc . ?tc a snomed:33195004 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .'
    assert run(check_code_system, chop, coded) == [
        "sphn:BilledProcedure codes are CHOP (https://biomedit.ch/rdf/sphn-resource/chop/), not SNOMED CT."
    ]
    namespace = (
        "SELECT ?concept (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?billed a sphn:BilledProcedure ; sphn:hasCode ?code . "
        '?code a ?concept . } FILTER (STRSTARTS(STR(?concept), "http://snomed.info/id/")) } GROUP BY ?concept'
    )
    assert len(run(check_code_system, namespace, coded)) == 1
    generic = GRAPH_COUNT % '?diag a sphn:Diagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:C18 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .'
    assert run(check_code_system, generic, coded) == [
        "sphn:Diagnosis codes are generic sphn:Code nodes in the data; ICD-10-GM codes are on sphn:BilledDiagnosis and sphn:OncologyDiagnosis."
    ]


def test_code_system_is_silent_when_the_system_matches_or_is_unknown(coded, schema):
    own = (
        "SELECT ?concept (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?proc a sphn:BilledProcedure ; sphn:hasCode ?code . "
        "?code a ?concept . } FILTER (STRSTARTS(STR(?concept), STR(chop:))) } GROUP BY ?concept"
    )
    assert run(check_code_system, own, coded) == []
    snomed = GRAPH_COUNT % "?t a sphn:RadiotherapyProcedure ; sphn:hasCode ?tc . ?tc a snomed:33195004 ."
    assert run(check_code_system, snomed, coded) == []
    chop = GRAPH_COUNT % "?t a sphn:BilledProcedure ; sphn:hasCode ?tc . ?tc a snomed:33195004 ."
    bare = cards({"BilledProcedure": {"hasCode": {"kind": "resource"}}})
    assert run(check_code_system, chop, bare) == []  # no code_system on the cards: nothing to say


def test_suggest_missing_term_renders_the_class_pattern_or_a_near_identical_property(schema):
    assert suggest_missing_term("hasSex", schema) == (
        "sphn:hasSex does not occur in the data. Sex is a record: ?s a sphn:AdministrativeSex ; sphn:hasCode ?c . "
        "?c a ?concept. Never return ?c."
    )
    assert suggest_missing_term("hasRankCode", schema) == "sphn:hasRankCode does not occur in the data. Did you mean sphn:hasRank?"
    age = suggest_missing_term("hasAge", schema)
    assert age.startswith(
        "sphn:hasAge does not occur in the data. Age is a record: ?s a sphn:Age ; sphn:hasQuantity ?quantity . ?quantity sphn:hasValue ?value."
    )
    assert "sphn:hasQuantity -> sphn:Quantity" in age
    diagnosis = suggest_missing_term("hasDiagnosis", schema)
    assert "?s a sphn:Diagnosis ; sphn:hasCode ?c . ?c a ?concept (also sphn:BilledDiagnosis)" in diagnosis
    assert suggest_missing_term("hasFooBarBaz", schema) == "sphn:hasFooBarBaz does not occur in the data."
    assert suggest_missing_term("hasSex", SchemaCards({}, [])) == "sphn:hasSex does not occur in the data."


def test_checks_are_silent_on_the_official_examples_and_without_cards(schema, places):
    for example in load_examples():
        text = add_missing_prefixes(example["sparql"].strip() + "\n")
        for rewrite in REWRITES:
            assert rewrite(text, schema) == (text, []), example["id"]
        parsed = parse_sparql(text)
        assert parsed.algebra is not None, example["id"]
        for check in CHECKS:
            assert check(parsed, text, schema) == [], (example["id"], check.__name__)
        for check in TERMINOLOGY_CHECKS:
            assert check(parsed, text, schema, places) == [], (example["id"], check.__name__)
    query = GRAPH_COUNT % "?stay a sphn:AdministrativeCase ; sphn:hasStartDateTime ?t . ?stay sphn:hasTherapeuticArea ?a . ?a a ?cls ."
    for check in CHECKS:
        assert run(check, query, SchemaCards({}, [])) == []
    for check in TERMINOLOGY_CHECKS:
        assert run(lambda parsed, text, cards: check(parsed, text, cards, places), query, SchemaCards({}, [])) == []
    assert rewrite_typed_value_set(query, SchemaCards({}, [])) == (query, [])


# --- A code whose kind lives in another class (18 October): "How many patients live in India?" typed the
# country code on OncologyDiagnosis, then on RadiotherapyProcedure, and answered 0 instead of 22. Fictional
# places and synthetic codes: no terminology label in the repo.
ATLANTIS_IRI = "http://snomed.info/id/1000101"
COUNTRY_USE = "sphn:Country hasCode (via Birth / HomeAddress hasCountry, Nationality hasAssociatedCountry)"
INFERENCE = 'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")'


@pytest.fixture(scope="module")
def places() -> TerminologyIndex:
    return TerminologyIndex([
        CodeEntry(ATLANTIS_IRI, "1000101", "SNOMED CT", ("Atlantis (geographic location)",)),
        CodeEntry("http://snomed.info/id/1000102", "1000102", "SNOMED CT", ("Atlantis fever (disorder)",)),
    ])


@pytest.fixture(scope="module")
def radiotherapy(tmp_path_factory) -> SchemaCards:
    """The cards with a closed list of codes on RadiotherapyProcedure.hasCode, as the local value sets have."""
    path = tmp_path_factory.mktemp("sets") / "value_sets.json"
    path.write_text(json.dumps({"classes": {"RadiotherapyProcedure": {"hasCode": [
        {"value": "snomed:1000201", "label": "Beam therapy", "count": 5},
        {"value": "snomed:1000202", "label": "Seed therapy", "count": 3},
    ]}}}), encoding="utf-8")
    return SchemaCards.load(DATA_DIR / "schema_cards.json", path)


def code_home(query: str, cards: SchemaCards, index: TerminologyIndex | None) -> list[str]:
    return run(lambda parsed, text, schema: check_code_home(parsed, text, schema, index), query, cards)


def test_code_home_names_the_class_a_code_of_a_closed_list_or_another_system_belongs_to(radiotherapy, places):
    on_procedure = GRAPH_COUNT % f"?c a snomed:1000101 {INFERENCE} . ?r sphn:hasCode ?c . ?r a sphn:RadiotherapyProcedure ."
    assert code_home(on_procedure, radiotherapy, places) == [
        f"snomed:1000101 is a geographic location, not a code of sphn:RadiotherapyProcedure hasCode: use it with {COUNTRY_USE}."
    ]
    # OncologyDiagnosis codes are ICD-10-GM: check_code_system says so, this check says where the code goes.
    on_diagnosis = GRAPH_COUNT % f"?c a snomed:1000101 {INFERENCE} . ?d sphn:hasCode ?c . ?d a sphn:OncologyDiagnosis ."
    assert code_home(on_diagnosis, radiotherapy, places) == [
        f"snomed:1000101 is a geographic location, not a code of sphn:OncologyDiagnosis hasCode: use it with {COUNTRY_USE}."
    ]
    assert len(run(check_code_system, on_diagnosis, radiotherapy)) == 1
    # Through VALUES too, and on a closed list of another coded property (Nationality.hasAcquistionCode).
    values = GRAPH_COUNT % "?r a sphn:RadiotherapyProcedure ; sphn:hasCode ?c . ?c a ?place . VALUES ?place { snomed:1000101 }"
    assert len(code_home(values, radiotherapy, places)) == 1
    acquired = SchemaCards(radiotherapy.classes, list(radiotherapy.schema_terms),
                           {**radiotherapy.value_sets, "Nationality": {"hasAcquistionCode": [
                               {"value": "snomed:1000301", "label": "Way one", "count": 1}]}})
    citizens = GRAPH_COUNT % "?n a sphn:Nationality ; sphn:hasAcquistionCode ?c . ?c a snomed:1000101 ."
    assert code_home(citizens, acquired, places) == [
        f"snomed:1000101 is a geographic location, not a code of sphn:Nationality hasAcquistionCode: use it with {COUNTRY_USE}."
    ]


def test_code_home_stays_silent_where_the_code_can_be(radiotherapy, places):
    right = GRAPH_COUNT % f"?addr a sphn:HomeAddress ; sphn:hasCountry ?country . ?country sphn:hasCode ?c . ?c a snomed:1000101 {INFERENCE} ."
    typed_country = GRAPH_COUNT % "?country a sphn:Country ; sphn:hasCode ?c . ?c a snomed:1000101 ."
    own = GRAPH_COUNT % "?r a sphn:RadiotherapyProcedure ; sphn:hasCode ?c . ?c a snomed:1000201 ."
    no_home = GRAPH_COUNT % "?r a sphn:RadiotherapyProcedure ; sphn:hasCode ?c . ?c a snomed:1000102 ."  # a disorder: no home
    unknown = GRAPH_COUNT % "?r a sphn:RadiotherapyProcedure ; sphn:hasCode ?c . ?c a snomed:1 ."  # not in the index
    untyped = GRAPH_COUNT % "?r sphn:hasCode ?c . ?c a snomed:1000101 ."
    open_list = GRAPH_COUNT % "?s a sphn:Substance ; sphn:hasCode ?c . ?c a snomed:1000101 ."  # SNOMED CT, no list: cannot tell
    for query in (right, typed_country, own, no_home, unknown, untyped, open_list):
        assert code_home(query, radiotherapy, places) == [], query
    on_procedure = GRAPH_COUNT % "?r a sphn:RadiotherapyProcedure ; sphn:hasCode ?c . ?c a snomed:1000101 ."
    assert code_home(on_procedure, radiotherapy, None) == []  # no index: no semantic tag
    assert code_home(on_procedure, SchemaCards({}, []), places) == []
    assert code_home(on_procedure, SchemaCards.load(DATA_DIR / "schema_cards.json"), places) == []  # no value sets: an open list


def test_the_judge_reports_a_code_typed_where_its_kind_cannot_be(radiotherapy, places):
    query = (
        "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { "
        f"?c a snomed:1000101 {INFERENCE} . ?r sphn:hasCode ?c . ?r a sphn:RadiotherapyProcedure . }} }}"
    )
    message = f"snomed:1000101 is a geographic location, not a code of sphn:RadiotherapyProcedure hasCode: use it with {COUNTRY_USE}."
    assert message in Judge(radiotherapy, places).check(query, "How many patients live in Atlantis?").errors
    assert message not in Judge(radiotherapy).check(query).errors  # the judge without an index


def test_module_lists_every_check_and_rewrite():
    assert CHECKS == [check_class_property, check_record_link_typed, check_typed_value_set, check_value_set_member, check_code_system]
    assert TERMINOLOGY_CHECKS == [check_code_home]
    assert REWRITES == [rewrite_typed_value_set, rewrite_year_versioned_code_key]
