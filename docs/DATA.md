# The SPHN data

How the patient knowledge graph is organized, with examples. It is written for teammates and for AI coding assistants; you do not need a database to read it. For the guard specifically, continue with [GUARD.md](GUARD.md).

## 1. Where the data lives

The data is **not in this repo**: the terminologies are restricted and the patient data weighs about 59 GB. It runs in Virtuoso on the run machine (Santiago's laptop):

| Database | Patients | Patient triples | Endpoint on the run machine |
|---|---|---|---|
| dev | 2,000 | 10.8 million | `http://localhost:8890/sparql` |
| perf | 100,000 | 611.7 million | `http://localhost:8891/sparql` |

Both databases also hold the ontologies: 13.0 million triples.

What *is* in the repo was derived from the dev database. None of it contains terminology content:

| File | What it holds |
|---|---|
| `data/examples.jsonl` | The 15 official example queries plus the worked example from the brief, each with its question and compliance label |
| `data/property_kinds.json` | For each of the 199 properties used in the data, what its objects are: value, value set or link |
| `data/schema_cards.json` | The 164 classes present in the data, with their properties, target classes and counts |
| `data/guard_corpus.jsonl` | 37 queries the LLM really wrote for our 31 test questions, with labels and the guard's verdict |

## 2. Two kinds of graphs

**Patient graphs.** Each patient is one named graph, for example `http://example.org/graph/gen_mock_data-1_1012502`.
- In queries this is always `GRAPH ?g { … }`.
- Count patients with `COUNT(DISTINCT ?g)`.
- Returning `?g`, or anything built from it, exposes the patient.

**The ontology graph** `https://biomedit.ch/rdf/sphn-resource/global` holds the SPHN schema and every terminology: ICD-10-GM, CHOP, SNOMED CT, LOINC, UCUM, ORDO, GENO, SO and OBI. It is also the RDFS inference rule set.

**Careful with the default graph.** In Virtuoso, a pattern written outside any `GRAPH` clause reads every graph, patient graphs included.

## 3. Anatomy of a patient graph

A shortened excerpt of a real mock patient. IRIs are abbreviated with `…`; `resource:` is `https://biomedit.ch/rdf/sphn-resource/`.

```turtle
# inside GRAPH <http://example.org/graph/gen_mock_data-1_1012502>

resource:CHE-…-sphn-BilledDiagnosis-7ae5… a sphn:BilledDiagnosis ;      # a record: its IRI is unique (UUID)
    sphn:hasCode resource:CHE-…-sphn-Code-ICD-10-GM-2022-A00.1 ;         # link to a code node
    sphn:hasRank sphn-individual:Complementary ;                          # value set
    sphn:hasRecordDateTime "1982-12-15T11:23:00+00:00"^^xsd:dateTime ;    # exact timestamp
    sphn:hasSubjectAge resource:CHE-…-sphn-Age-fe76… ;                    # link to another record
    sphn:hasAdministrativeCase resource:CHE-…-sphn-AdministrativeCase-dddd… ;
    sphn:hasSubjectPseudoIdentifier resource:CHE-…-sphn-SubjectPseudoIdentifier-d1e2… .

resource:CHE-…-sphn-Code-ICD-10-GM-2022-A00.1
    a <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/2022/A00.1> .     # the code is the node's TYPE

resource:CHE-…-sphn-SubjectPseudoIdentifier-d1e2… a sphn:SubjectPseudoIdentifier ;
    sphn:hasIdentifier "ID-10125020000015" ;                              # identifier: never expose
    sphn:hasSharedIdentifier "…"^^xsd:anyURI .                            # identifier too

resource:CHE-…-sphn-AdministrativeSex-0d71… a sphn:AdministrativeSex ;
    sphn:hasCode resource:CHE-…-sphn-Code-SNOMED-CT-248153007 .           # typed snomed:248153007 = male
```

Records point to other records: a diagnosis points to an administrative case, an age, a pseudo-identifier. Every record IRI contains a UUID, so **record IRIs identify a patient** as much as their identifiers do.

## 4. What kind of value each property holds

This is what the guard relies on. It comes from `data/property_kinds.json`, measured on the data.

| Kind | Properties | Examples | What the object is |
|---|---|---|---|
| `literal` | 43 | `hasValue` (double), `hasRecordDateTime`, `hasStartDateTime` (dateTime), `hasName` (string), `hasYear` (gYear) | A value. Dates and times are exact timestamps |
| `individual` | 16 | `hasRank`, `hasOutcome`, `hasConsequences`, `hasCoverageType`, `hasComparator`, `hasCategory` | A value-set entry such as `sphn-individual:Principal`: a category |
| `resource` | 138 | `hasCode`, `hasAdministrativeCase`, `hasSubjectAge`, `hasQuantity`, `hasUnit`, `hasDrug` | A link to another record or to a code node: an IRI unique to one patient |
| `mixed` | 2 | `rdf:type`, `dct:conformsTo` | — |

**Identifiers:** `hasIdentifier` (266,000 values on dev), `hasSharedIdentifier`, and any property whose name contains `Identifier`. The guard treats all of them as identifiers.

**Classes:** the object of `rdf:type` is always a class or a code concept, never a patient record.

## 5. Codes and terminologies

Pattern used everywhere:

```sparql
?diagnosis sphn:hasCode ?code .
?code a icd-10-gm:E11 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .
```

- **Codes carry a year** (`icd-10-gm/2022/A00.1`). The inference option makes `icd-10-gm:A00` match every year and every subcode. Without it nothing matches.
- **Codes used in the examples:**
  - `icd-10-gm:A00`: cholera.
  - `icd-10-gm:E11`: type 2 diabetes.
  - `chop:00.0`: therapeutic ultrasound.
  - `snomed:248153007`, `snomed:248152002` and `snomed:32570681000036106`: male, female and indeterminate sex.
- **Label languages:**
  - ICD-10-GM labels are in German and French ("Diabetes mellitus, Typ 2").
  - CHOP labels are in German, French and Italian.
  - SNOMED CT labels are in English.
  - Requests arrive in English.

| Prefix | Namespace |
|---|---|
| `sphn:` | `https://biomedit.ch/rdf/sphn-schema/sphn#` |
| `sphn-individual:` | `https://biomedit.ch/rdf/sphn-schema/sphn/individual#` |
| `resource:` | `https://biomedit.ch/rdf/sphn-resource/` (records and code nodes) |
| `icd-10-gm:` | `https://biomedit.ch/rdf/sphn-resource/icd-10-gm/` |
| `chop:` | `https://biomedit.ch/rdf/sphn-resource/chop/` |
| `snomed:` | `http://snomed.info/id/` |
| `loinc:` | `https://loinc.org/rdf/` |
| `ucum:` | `https://biomedit.ch/rdf/sphn-resource/ucum/` |
| `bif:` | `bif:` (Virtuoso built-in functions such as `bif:datediff`) |

## 6. The house style

These shapes come from the official examples. A compliant query always ends in aggregates.

**Count patients with a code** (the worked example from the brief):
```sparql
SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g {
  ?code a icd-10-gm:E11 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .
  ?dx sphn:hasCode ?code . ?dx a sphn:BilledDiagnosis . } }
```

**A distribution by category.** Group by a class or a value set, and label codes with `VALUES` (example #03):
```sparql
SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) WHERE {
  GRAPH ?g { ?r a sphn:AdministrativeSex ; sphn:hasCode ?c . ?c a ?concept . }
  VALUES (?concept ?sex) { (snomed:248153007 "male") (snomed:248152002 "female") }
} GROUP BY ?sex
```

**Conditional counts in one pass** (examples #03, #08, #09). `?x` only counts the patients who match:
```sparql
OPTIONAL { GRAPH ?g { ?event sphn:hasOutcome sphn-individual:Fatal . } BIND (?g AS ?fatal_patient) }
… (COUNT(DISTINCT ?fatal_patient) AS ?patients_fatal_outcome)
```

**Durations:** `BIND (bif:datediff("day", ?start, ?end) AS ?days)` (examples #01, #02, #06, #07).

**Ontology lookups** happen in the ontology graph only:
```sparql
GRAPH <https://biomedit.ch/rdf/sphn-resource/global> { ?concept rdfs:label ?label }
```

**Performance hints:**
- `DEFINE sql:select-option "order"` makes Virtuoso keep the written pattern order, so put the most selective pattern first.
- Use inference only on code patterns: it is expensive.

## 7. Facts and quirks

- **The data does not follow the 2026.1 schema everywhere.** The schema says `sphn:hasRankCode`; the data uses `sphn:hasRank`. Trust `data/schema_cards.json`, which is measured on the data.
- **Every patient has a fixed backbone:** AdministrativeSex, Birth, BirthDate, Age, AdministrativeCase, Consent, Nationality, InsuranceStatus and more. Clinical classes vary a lot: OncologyDiagnosis appears in 78 of 2,000 patients, and genomics is just as rare.
- **The data is synthetic.** Free text is lorem ipsum and 38% of patients have cholera, so do not read medical meaning into the numbers.
- **Patient sizes vary** from 31 KB to 506 MB per file. A few huge patients dominate some queries.
- **Size changes everything.** A query that takes milliseconds on dev can take more than 30 minutes on perf: see the timing table in the README, section 11.

## 8. What a schema card says

`data/schema_cards.json` is measured class by class by `scripts/profile_data.py` (`--cards-only` keeps `property_kinds.json` and rebuilds the cards in about a minute; every query runs under Virtuoso's 30 s anytime timeout and a partial answer is an error). The query agent sees the cards the picker selects, rendered one per line:

```
sphn:BilledDiagnosis [2026 records, 1008 patients]: hasAdministrativeCase -> sphn:AdministrativeCase; hasCode -> ICD-10-GM code (?c a ?concept; one code from the candidates to match a concept); ...; hasRank = one of sphn-individual:Principal | sphn-individual:Complementary | sphn-individual:Secondary; hasRecordDateTime (dateTime); ...
```

Per property the card holds, besides `kind` and `count`:

| Field | Meaning | Rendered as |
|---|---|---|
| `datatype` | the literal's XSD type | `hasRecordDateTime (dateTime)` |
| `target`, `targets` | the class a link reaches; `targets` lists every class observed, most frequent first (40 links reach several, e.g. `LabTestEvent.hasSample` -> Sample, Isolate, TumorSpecimen; a card prints at most 12) | `hasSample -> sphn:Sample \| sphn:Isolate \| sphn:TumorSpecimen` |
| `code_system` | the terminology of the code nodes: `ICD-10-GM` (BilledDiagnosis, OncologyDiagnosis), `CHOP` (BilledProcedure), `SNOMED CT` (38 classes), `GENO`, a namespace, or `generic` when the nodes carry only `sphn:Code` (17 classes: Diagnosis, NursingDiagnosis, MedicalProcedure, AdverseEvent, LabResult, ...). `code_systems` gives the split when a property mixes them (Substance.hasCode: 95% SNOMED CT) | `hasCode -> CHOP code (?c a ?concept; one code from the candidates to match a concept)` / `hasCode -> SNOMED CT code (95%, the rest generic sphn:Code; ?c a ?concept; ...)` / `hasCode -> generic sphn:Code (no terminology type: cannot be matched by ICD/CHOP/SNOMED)` |
| `values` | the members of an sphn-individual value set with their counts, in full (TherapeuticArea.hasSpecialtyName has 64) | `hasSpecialtyName = one of sphn-individual:Urology \| ...` |

A coded property shows no example code and no inference option: the model copied the former placeholder `?c a snomed:<code> OPTION (inference ...)` as it stood (11 dev answers of the 17 October run 3, all unparseable), so the card names the `?c a ?concept` pattern and points at the code candidates, and the prompt rules say how a concrete code is typed with the inference option. `hasSubjectPseudoIdentifier`, which 79 classes carry, is rendered only on the SubjectPseudoIdentifier card (`... (every record links to it with hasSubjectPseudoIdentifier): hasIdentifier (string); ...`): one patient is one graph, no compliant reference joins through it, and the join was wrong or harmful in three dev answers (official-04, q-029, q-042). The judge still reads the property from `schema_cards.json`, so a query that uses it is checked as before.

A card that uses a prefix the prompt does not declare prints its namespace once: `(geno: <http://purl.obolibrary.org/obo/GENO_>)`. With `assets/value_sets.json` present (built by `scripts/build_value_sets.py`, never committed), coded sets of at most 20 members are rendered with their labels: `hasCode -> code typed as one of Married snomed:... | ...`. The cap of 20 (15 until 17 October) takes in the procedure intents (Intent.hasCode, 17 members) and the tumour grades (18); the next coded sets have 21 members or more.

**How the picker chooses** (`SchemaCards.cards_for`, `CONTEXT_CARDS` = 8): whole-word matches of class names (stemmed, so "stays" meets "stay"; words shared by many names such as "medical" or "event" weigh half), a synonym table for backbone classes (stay/admission/in hospital/inpatient -> AdministrativeCase, citizen -> Nationality, BMI, blood pressure, died/death -> Death, cancer/tumour -> OncologyDiagnosis, ingredient -> Substance, specialty -> TherapeuticArea, follow-up -> FollowUp and VitalStatus, born/baby -> Birth, early/preterm/gestational -> GestationalAgeAtBirth, sex/male/female -> AdministrativeSex, "give drugs"/administered/route/by mouth/IV -> DrugAdministrationEvent, intent/guidance/screening/diagnostic -> Intent, identifier/pseudonym/patient id -> SubjectPseudoIdentifier), property names ("active ingredient" -> Drug), value labels ("alive" -> VitalStatus), the classes the retrieved examples use, and one link from a shown card when the question names the link or its target (FollowUp.hasResult -> VitalStatus), or when a code candidate is a member of the target's value set (the screening-intent code makes ImagingProcedure.hasIntent pull Intent). An explicit `sphn:Name` always ranks first; spare slots go to the classes the best card links to; provenance classes (SourceSystem, DataProvider, SubjectPseudoIdentifier) are never pulled through a link or through an example (official-15 uses SubjectPseudoIdentifier, and it used to reach allowed questions that way): only a question that names identifiers gets that card. On the 16 official and 45 dev reference questions the cards cover 97 of the 98 classes the reference queries use (94 of 98 before the 17 October evening changes; the miss is Age for official-12, a forbidden request); with the 3 team examples, 106 of 107.

**Code candidates** (`TerminologyIndex.candidates`, `CONTEXT_CODES` = 5): a candidate whose label only spells out a class name of two or more words, semantic tag included ("Imaging (procedure)" for sphn:ImagingProcedure), is dropped and the next one takes its slot: no reference uses such a code, and the model typed the intent of an imaging procedure with it twice (q-057, q-074). On the 64 reference questions it drops exactly that one label (q-057, q-062, q-066, q-074) and no code a reference needs.

## 9. Exploring the data yourself

Ask Santiago to run queries on the run machine, or to give you access to it. Always use the HTTP endpoint: the release's `query.sh` (isql) rejects some valid SPARQL. Two useful starting points:

```sparql
# Which classes exist, in how many patients?
SELECT ?class (COUNT(DISTINCT ?g) AS ?patients)
WHERE { GRAPH ?g { ?s a ?class } FILTER(STRSTARTS(STR(?class), "https://biomedit.ch/rdf/sphn-schema/sphn#")) }
GROUP BY ?class ORDER BY DESC(?patients)

# Which properties does a class use?
SELECT ?p (COUNT(*) AS ?n)
WHERE { GRAPH ?g { ?s a sphn:LabTestEvent ; ?p ?o } } GROUP BY ?p ORDER BY DESC(?n)
```

Most of these answers are already in `data/schema_cards.json`. Look there first.
