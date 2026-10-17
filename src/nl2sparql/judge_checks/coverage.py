"""Judge checks that read the question: the query must answer what it asks.

Two checks, both exported through CHECKS with the signature (question, parsed, text, schema, terminology):
check_question_coverage, the coded concepts the question names, and check_record_count_asked, the
record count the question asks for. Each one costs the request its only judge retry when it fires, so
both prefer a miss to a false alarm.

Concept coverage. The console showed "How many patients have cholera on a hospital bill, by sex?"
answered with all patients by sex: the query agent dropped the cholera pattern, and no other check
compares the codes the question names with the query. The terminology index (context/terminology.py)
maps the question's words to the code candidates of the prompt; this check takes the candidates the
question names strongly and requires the query to use that code, a subcode or a parent code of the
same system anywhere: a typed pattern, VALUES, a FILTER, or a string that spells the code or its name.

A false alarm costs the request its only judge retry and a miss costs nothing, so a match is strong only
when it cannot be a coincidence:
- an ICD-10-GM code written in the question ("E11"), or
- an ICD-10-GM or CHOP label whose words all occur side by side in the question ("therapeutic
  ultrasound", "Type 2 diabetes mellitus"), stop words skipped and simple plurals folded as the index
  does. A one-word label counts only when two labels of the code agree on it (Cholera / Choléra): the
  labels are German, French and Italian plus a few English aliases, and one foreign word that happens to
  be English is noise ("triples" finds the French "Triplés", O30.1).
Not strong:
- one word that only implies the rest of its label (the index's rule for the prompt: "diabetes" finds
  "Diabetes mellitus"). It is a co-occurrence share measured for retrieval, and on everyday words it
  reaches labels the question does not name (the dev questions' "testing" found "Synergism testing" and
  "place" found "Place of occurrence of accident or poisoning"); among ICD-10-GM and CHOP labels it would
  only have added diabetes on the dev questions, which every generated query had;
- label words scattered over the question ("inpatient, outpatient and day-care" is not "Inpatient care");
- SNOMED CT. Its entries in the index are the concepts the data uses, mostly categories of coded
  properties. Of the 68 SNOMED CT labels the 197 dev questions contain side by side, 62 are one everyday
  word ("other patients", "deaths", "blood pressure", "married"); the six longer ones ("indeterminate sex",
  "lost to follow-up", "right and left") are categories that a GROUP BY over the code covers as well as
  VALUES does, and every reference and generated query had them.
A concept the question names with the words of an SPHN class (its name parts, or the card picker's
synonyms: "cancer" -> OncologyDiagnosis) is left alone: the class may be the handle.

Measured with the real index (17 October): zero hits on the 16 official examples, the 3 team examples and
the 45 dev reference queries, each against its own question; on the dev rows of the query and pipeline
evals, one hit on the final queries (official-06, which filters on a CHOP namespace that does not exist
and returns nothing) and none on the first tries. tests/test_judge_coverage.py builds a small index.

Record count. "How many drug prescriptions are for continuous dosing ...? Give the number of patients
for each as well" came back with the patients only, and "How many allergy episodes are confirmed ..."
with the patients counted under ?episodes: the second failure analysis found the asked record count
missing or counted on the wrong unit in ten of the dev answers, all wrong. The question asks for a
record count when a cue ("how many", "the number of", "give") is followed by at most two modifiers and
a record noun. The nouns come from the schema cards: the head word of every class that carries
hasSubjectPseudoIdentifier, which is how SPHN marks the records of a patient (procedure, prescription,
diagnosis, episode, event, case, encounter, death, ...), plus the everyday plurals the class names do
not spell (stays, admissions, visits, tests, follow-ups, records). A patient word among the modifiers
makes it a patient count ("how many patients with a prescription", "how many of them"), and a kind word
a count of categories ("how many different procedures"). The query then has to count records
somewhere: COUNT(*), or a COUNT over a variable the query types (?x a sphn:Class, or ?x a ?class), a
code node of a record (one node per record) or an alias of either (BIND(?record AS ?x)); a per-patient
count in a sub-select counts too, because "how many prescriptions does a patient have on average" is
answered that way. COUNT(DISTINCT ?g) and its aliases count patients. The hint names the question's
own words, and the query's record variable when its class matches them ("imaging procedures" ->
?procedure a sphn:ImagingProcedure) or when it is the only typed variable.

Measured on the same references: the cue matches 23 of the 45 dev questions, official-11 and
team-ex-01, and every one of their reference queries counts records (0 false alarms). On the dev rows
of the 17 October query eval: 10 final queries, all wrong (q-003, q-005, q-011, q-042, q-049, q-054,
q-055, q-057, q-066, q-068) and 8 first tries, none of them a right answer; the hint applied by hand
to the finals of q-049, q-042 and q-003 gives exact, exact_numbers and exact_numbers on dev.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF

from nl2sparql.context.bm25 import STOPWORDS, TOKEN_RE, fold
from nl2sparql.context.schema import IRREGULAR_PLURALS, PROVENANCE_CLASSES, SYNONYMS, SchemaCards, stem, words
from nl2sparql.context.terminology import ICD_CODE_RE, SEMANTIC_TAG_RE, TerminologyIndex, label_words, parent_code
from nl2sparql.judge_checks.semantics import (
    CODE_PROPERTY_RE, GRAPH, RECORD_WORDS, _alias_words, _column_aggregates, _mentions, _nodes, _triples,
)
from nl2sparql.judge_checks.scope import _is_projected_key
from nl2sparql.models import CodeEntry
from nl2sparql.sparql.parse import ParsedQuery
from nl2sparql.sparql.prefixes import INFERENCE_RULE_SET, SPHN, STANDARD_PREFIXES
from nl2sparql.sparql.terms import collect_iris

# The systems the check holds the query to: the prefix the hint writes and the record variable it names.
SYSTEMS = {"ICD-10-GM": ("icd-10-gm", "dx"), "CHOP": ("chop", "procedure")}
# Where each system's codes live; SNOMED CT only to recognize the same concept under another system.
NAMESPACES = {
    STANDARD_PREFIXES["icd-10-gm"]: "ICD-10-GM",
    STANDARD_PREFIXES["chop"]: "CHOP",
    STANDARD_PREFIXES["snomed"]: "SNOMED CT",
}
MIN_WORD = 4  # as in TerminologyIndex.candidates: a label needs one word of four letters or more
MIN_AGREEING_LABELS = 2  # a one-word label: Cholera and Choléra, never one foreign word
MIN_SPELLED_PARENT = 3  # a string that spells a parent code: not a chapter ("00" is in every time "00:00")
YEAR_RE = re.compile(r"^\d{4}/")  # the data's code classes carry a year: icd-10-gm/2022/A00.1
BLOCK_RE = re.compile(r"^([A-Z]\d{2})-([A-Z]\d{2})$")  # an ICD-10-GM block: E10-E14
SPAN_RE = re.compile(r"[\w\s-]+")  # words, spaces and hyphens: "non-insulin-dependent diabetes mellitus"
ICD_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9.])([A-Z]\d{2}(?:-[A-Z]\d{2}|\.\d{1,2})?)(?!\d)")  # E11, E11.9, E10-E14

# The cue of a record count, then at most MAX_MODIFIERS words and the record noun: "how many imaging
# procedures", "the number of follow-ups", "give procedures and patients per side".
CUE_RE = re.compile(r"\b(?:how many|number of|give)\b")
QUESTION_WORD_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # "follow-ups" and "day-care" stay one word
MAX_MODIFIERS = 2
RECORD_MARKER = "hasSubjectPseudoIdentifier"  # the property every patient-level SPHN record carries
# Count nouns for records that the class names do not spell: the stay (AdministrativeCase), the visit
# (HealthcareEncounter), the administration (DrugAdministrationEvent), the lab test (LabTestEvent).
COMMON_RECORD_NOUNS = frozenset({
    "record", "stay", "admission", "hospitalization", "hospitalisation", "visit", "administration", "test",
    "transplant", "implant", "follow-up", "followup",
})
# Head words of record classes that name an attribute of the patient: "how many nationalities" asks for kinds.
ATTRIBUTE_WORDS = frozenset({"sex", "age", "date", "status", "nationality"})
# A modifier that turns the cue into a patient count or a count of categories.
PATIENT_WORDS = frozenset({
    "patient", "patients", "pt", "pts", "people", "person", "persons", "subject", "subjects", "individual",
    "individuals", "man", "men", "woman", "women", "baby", "babies", "of", "them", "these", "those", "their",
})
KIND_WORDS = frozenset({"different", "distinct", "unique", "separate", "various", "kind", "kinds", "type", "types"})
DETERMINERS = frozenset({"the", "a", "an", "all", "any", "such", "total"})  # modifiers the hint does not repeat
NOUN_CLASSES = {"visit": ("HealthcareEncounter",)}  # count nouns the card picker's SYNONYMS do not map


@dataclass(frozen=True)
class _Match:
    entry: CodeEntry
    start: int  # the first question token of the match: the hints follow the question's order
    name: str  # the question's own words for the concept
    words: frozenset[str]  # the label words the question matched (none for a written code)


def check_question_coverage(question: str, parsed: ParsedQuery, text: str, schema: SchemaCards,
                            terminology: TerminologyIndex | None) -> list[str]:
    """One hint per coded concept the question names strongly and the query never uses."""
    if not question or terminology is None or not terminology.entries or parsed.algebra is None:
        return []
    matches = _strong_matches(question, terminology, schema)
    if not matches:
        return []
    iris = collect_iris(parsed.algebra)
    codes = _codes(iris)
    strings = [str(term).replace("\\", "") for term in _literals(parsed.algebra)]  # REGEX "^00\\.0" spells 00.0
    return [_hint(match) for match in matches if not _covered(match, codes, strings, terminology)]


# ------------------------------------------------------------------------------- question side
def _strong_matches(question: str, index: TerminologyIndex, schema: SchemaCards) -> list[_Match]:
    """The codes the question names strongly, the most specific of each family, in question order."""
    tokens, raw, spans = _question_tokens(question)
    present = set(tokens)
    found: dict[tuple[str, str], _Match] = {}
    for written in ICD_CODE_RE.finditer(question):
        entry = index.by_code.get(written.group(1))
        if entry is not None and entry.system == "ICD-10-GM":
            found.setdefault((entry.system, entry.code), _Match(entry, -1, written.group(1), frozenset()))
    for label in sorted({label for word in present for label in index.postings.get(word, ())}):
        position, matched = index.labels[label]
        entry = index.entries[position]
        if entry.system not in SYSTEMS or not matched <= present or not any(len(word) >= MIN_WORD for word in matched):
            continue
        if len(matched) == 1 and not _agreed(entry, matched):
            continue
        start = _window(tokens, matched)
        known = found.get((entry.system, entry.code))
        if start is None or (known is not None and known.start <= start):
            continue
        if not _names_a_class(raw[start:start + len(matched)], schema):
            name = _span(question, spans, start, len(matched)) or _label_text(entry, matched)
            found[(entry.system, entry.code)] = _Match(entry, start, name, frozenset(matched))
    specific = [
        match for match in found.values()
        if not any(other.entry.system == match.entry.system and _is_ancestor(match.entry.code, other.entry.code)
                   for other in found.values())
    ]
    return sorted(specific, key=lambda match: (match.start, match.entry.code))


def _question_tokens(question: str) -> tuple[list[str], list[str], list[tuple[int, int] | None]]:
    """The question's words without stop words: as the index folds them (smokers -> smoker), as written
    (lowercase, without accents), and their character spans when folding kept the text's length (accents
    do; a ligature does not, and then the hint names the label instead)."""
    folded = fold(question)
    aligned = len(folded) == len(question)
    tokens, raw, spans = [], [], []
    for match in TOKEN_RE.finditer(folded):
        if match.group() in STOPWORDS:
            continue
        tokens.append(next(iter(label_words(match.group()))))  # the index's own plural folding
        raw.append(match.group())
        spans.append(match.span() if aligned else None)
    return tokens, raw, spans


def _agreed(entry: CodeEntry, matched: set[str]) -> bool:
    """A one-word label counts when two labels of the code come down to that word (Cholera, Choléra)."""
    return sum(label_words(label) == matched for label in entry.labels) >= MIN_AGREEING_LABELS


def _window(tokens: list[str], matched: set[str]) -> int | None:
    """Where the label's words stand side by side in the question, in any order."""
    size = len(matched)
    for start in range(len(tokens) - size + 1):
        if set(tokens[start:start + size]) == matched:
            return start
    return None


def _span(question: str, spans: list, start: int, size: int) -> str | None:
    """The question's own words for the match, when they are only the label's words ("Type 2 diabetes
    mellitus"); None when stop words or punctuation stand between them ("cholera, how many El Tor")."""
    first, last = spans[start], spans[start + size - 1]
    if first is None or last is None:
        return None
    text = question[first[0]:last[1]]
    return text if SPAN_RE.fullmatch(text) and len(TOKEN_RE.findall(fold(text))) == size else None


def _label_text(entry: CodeEntry, matched: set[str]) -> str:
    label = next((text for text in entry.labels if label_words(text) == matched), entry.labels[0])
    return SEMANTIC_TAG_RE.sub("", label)


def _names_a_class(question_words: list[str], schema: SchemaCards) -> bool:
    """True when the words name an SPHN class of the data: every part of its name, or a synonym the card
    picker maps to it ("cancer" -> OncologyDiagnosis). Stemmed as the card picker stems."""
    if schema.empty:
        return False
    stems = [stem(word) for word in question_words]
    phrases = set(stems) | {f"{first} {second}" for first, second in zip(stems, stems[1:])}
    for synonyms, classes in SYNONYMS.items():
        if any(cls in schema.classes for cls in classes) and any(
            " ".join(stem(word) for word in synonym.split()) in phrases for synonym in synonyms
        ):
            return True
    return any({stem(part) for part in words(cls)} <= phrases for cls in schema.classes)


# ---------------------------------------------------------------------------------- query side
def _codes(iris: set[URIRef]) -> set[tuple[str, str]]:
    """(system, code) for every terminology IRI of the query, without the year of a data class."""
    found = set()
    for iri in iris:
        for namespace, system in NAMESPACES.items():
            if str(iri).startswith(namespace):
                code = YEAR_RE.sub("", str(iri)[len(namespace):])
                if code:
                    found.add((system, code))
    return found


def _literals(node) -> list[Literal]:
    """Every literal of the algebra: FILTER strings, VALUES labels, BIND constants."""
    found: list[Literal] = []
    if isinstance(node, Literal):
        found.append(node)
    elif isinstance(node, (list, tuple, set)):
        for item in node:
            found += _literals(item)
    elif isinstance(node, dict):  # CompValue nodes and VALUES rows
        for key, value in node.items():
            if not (isinstance(key, str) and key.startswith("_")):
                found += _literals(value)
    return found


def _covered(match: _Match, codes: set[tuple[str, str]], strings: list[str], index: TerminologyIndex) -> bool:
    """True when the query uses the code or a relative, the concept under another system, or a string
    that spells the code or a word of its labels (FILTER (CONTAINS(LCASE(?label), "cholera")))."""
    entry = match.entry
    for system, code in codes:
        if system == entry.system and _related(code, entry.code):
            return True
        if match.words and _same_concept(system, code, match.words, index):
            return True
    names = {word for label in entry.labels for word in label_words(label)
             if len(word) >= MIN_WORD and not word.isdigit()}
    return any(_spells(string, entry.code) or names & label_words(string) for string in strings)


def _related(code: str, other: str) -> bool:
    """The same code, a subcode or a parent code: A00 covers A00.0, A00.1, A00.9 and the reverse."""
    return code == other or _is_ancestor(code, other) or _is_ancestor(other, code)


def _is_ancestor(code: str, other: str) -> bool:
    """True when other sits below code: E11 above E11.0, 00.0 above 00.01, the block E10-E14 above E11."""
    block = BLOCK_RE.match(code)
    if block:
        return code != other and block.group(1) <= other[:3] <= block.group(2)
    parent = parent_code(other)
    while parent is not None:
        if parent == code:
            return True
        parent = parent_code(parent)
    return False


def _same_concept(system: str, code: str, matched: frozenset[str], index: TerminologyIndex) -> bool:
    """The question's concept under another system: a code of the index whose label has the same words."""
    entry = index.by_code.get(code)
    return entry is not None and entry.system == system and any(matched <= label_words(text) for text in entry.labels)


def _spells(string: str, code: str) -> bool:
    """A string that spells the code, a subcode or a parent code: STRSTARTS(STR(?c), ".../A00"),
    REGEX(?code, "^00\\.0"), an ICD-10-GM block around the code or a code inside the block."""
    if re.search(rf"(?<![A-Za-z0-9.]){re.escape(code)}", string):
        return True
    if any(_related(found, code) for found in ICD_IN_TEXT_RE.findall(string)):
        return True
    parent = parent_code(code)
    while parent is not None and len(parent) >= MIN_SPELLED_PARENT:
        if re.search(rf"(?<![A-Za-z0-9.]){re.escape(parent)}(?![0-9A-Za-z])", string):
            return True
        parent = parent_code(parent)
    return False


def _hint(match: _Match) -> str:
    prefix, record = SYSTEMS[match.entry.system]
    concept = f"{prefix}:{match.entry.code}"
    return (
        f"The question asks about {match.name} ({concept}), but the query never restricts to it. "
        f"Add it to the record the question is about: ?{record} sphn:hasCode ?code . "
        f'?code a {concept} OPTION (inference "{INFERENCE_RULE_SET}") .'
    )


# ------------------------------------------------------------------------------- the record count
@dataclass(frozen=True)
class _Asked:
    phrase: str  # the question's words for the records: "imaging procedures", "follow-ups"
    stems: frozenset[str]  # the stems of those words, to find the query's record variable


@dataclass(frozen=True)
class _Counts:
    """What the query's COUNT aggregates count, and what nothing binds."""
    records: bool  # some COUNT counts records (see _count_analysis)
    patients: Variable | None  # the alias of the outermost COUNT(DISTINCT ?g), or of a count of patients
    unbound: tuple[Variable, ...]  # COUNT inputs that nothing binds, in query order
    projected_unbound: frozenset[Variable]  # projected variables that nothing binds
    grouped: frozenset[Variable]  # GROUP BY keys


def check_record_count_asked(question: str, parsed: ParsedQuery, text: str, schema: SchemaCards,
                             terminology: TerminologyIndex | None = None) -> list[str]:
    """One hint when the question asks how many records and the query counts none of them. The terminology
    is accepted so the judge can loop over CHECKS; this check does not read it."""
    if not question or parsed.algebra is None:
        return []
    asked = _asked_records(question, _record_nouns(schema))
    if asked is None:
        return []
    counts = _count_analysis(parsed.algebra, schema)
    if counts.records:
        return []
    return [_record_hint(asked, counts, parsed.algebra, text, schema)]


def _record_nouns(schema: SchemaCards) -> set[str]:
    """The singular nouns a question counts records with: the head word of every class that carries the
    record marker (FollowUp, whose head is not a word, is joined: follow-up), plus the common ones."""
    nouns = set(COMMON_RECORD_NOUNS)
    for name, card in schema.classes.items():
        if RECORD_MARKER not in card["properties"]:
            continue
        parts = words(name)
        if len(parts) > 1 and len(parts[-1]) <= 2:
            nouns.update(("-".join(parts), "".join(parts)))
        elif parts[-1] not in ATTRIBUTE_WORDS:
            nouns.add(parts[-1])
    return nouns


def _asked_records(question: str, nouns: set[str]) -> _Asked | None:
    """The records the question asks to count: the first cue followed by at most MAX_MODIFIERS words and
    a plural record noun, with no patient or kind word between, inside one clause."""
    lowered = re.sub(r"\b(\w+) (ups?)\b", r"\1-\2", question.lower())  # "follow ups" -> "follow-ups"
    for cue in CUE_RE.finditer(lowered):
        clause = re.split(r"[.,;:?!]", lowered[cue.end():], maxsplit=1)[0]
        following = QUESTION_WORD_RE.findall(clause)[: MAX_MODIFIERS + 1]
        for position, word in enumerate(following):
            if word in PATIENT_WORDS or word in KIND_WORDS:
                break
            singular = _singular(word)
            if singular is not None and singular in nouns:
                modifiers = [modifier for modifier in following[:position] if modifier not in DETERMINERS]
                stems = {stem(part) for modifier in modifiers + [singular] for part in modifier.split("-")}
                stems.add(singular.replace("-", ""))  # follow-up: the query may spell it ?followup
                return _Asked(" ".join(modifiers + [word]), frozenset(stems))
    return None


def _singular(word: str) -> str | None:
    """The singular of a plural noun (episodes -> episode, diagnoses -> diagnosis); None for any other word,
    so that "recorded" never passes as the noun "record"."""
    if word in IRREGULAR_PLURALS:
        return IRREGULAR_PLURALS[word]
    if not word.endswith("s") or word.endswith(("ss", "us", "is")):
        return None
    singular = stem(word)
    return singular if singular != word else None


def _count_analysis(algebra, schema: SchemaCards) -> _Counts:
    """A COUNT counts records when it is COUNT(*) or reads a node of the data: a variable the query types
    (?x a sphn:Class, ?x a ?class, ?code a icd-10-gm:A00), the subject of an sphn property, the object of a
    link or code property (one node per record; the shared provenance nodes excepted), or an alias of one
    (BIND(?record AS ?x)). ?g, the other GRAPH variables and their aliases count patients."""
    nodes = _nodes(algebra)
    node_properties = _node_properties(schema)
    bound, records, patients = set(), set(), {GRAPH}
    for subject, predicate, obj in _triples(algebra):
        bound.update(term for term in (subject, predicate, obj) if isinstance(term, Variable))
        if isinstance(subject, Variable) and (predicate == RDF.type or str(predicate).startswith(SPHN)):
            records.add(subject)
        name = str(predicate)[len(SPHN):] if str(predicate).startswith(SPHN) else None
        if isinstance(obj, Variable) and name and (name in node_properties or CODE_PROPERTY_RE.match(name)):
            records.add(obj)
    grouped = set()
    for node in nodes:
        if node.name == "values":
            bound.update(variable for row in node.res for variable in row)
        elif node.name == "Graph" and isinstance(node.get("term"), Variable):
            bound.add(node.term)
            patients.add(node.term)
        elif node.name == "Extend" and not str(node.var).startswith("__agg") and not _is_projected_key(node):
            bound.add(node.var)  # a projected GROUP BY key is Extend(?key, SAMPLE(?key)) and binds nothing new
        elif node.name == "Group" and isinstance(node.expr, list):
            grouped.update(key for key in node.expr if isinstance(key, Variable))
    aliases = [(node.var, node.expr) for node in nodes if node.name == "Extend" and isinstance(node.expr, Variable)]
    for found in (records, patients):
        grown = True
        while grown:  # BIND(?record AS ?x), and BIND(?x AS ?y) after it
            grown = False
            for variable, source in aliases:
                if source in found and variable not in found:
                    found.add(variable)
                    grown = True
    counts_records, unbound, patient_alias = False, [], None
    for node in nodes:
        if node.name != "AggregateJoin":
            continue
        for aggregate in node.A:
            if aggregate.name != "Aggregate_Count":
                continue
            variable = aggregate.vars if isinstance(aggregate.vars, Variable) else None
            if aggregate.vars == "*" or variable in records:
                counts_records = True
            elif variable is not None and variable not in bound and variable not in unbound:
                unbound.append(variable)
    for _, alias, aggregate in _column_aggregates(algebra):  # the outermost SELECT first
        if aggregate.name == "Aggregate_Count" and isinstance(aggregate.vars, Variable) and aggregate.vars in patients:
            patient_alias = alias
            break
    projected_unbound = {variable for node in nodes if node.name == "Project" for variable in node.PV if variable not in bound}
    return _Counts(counts_records, patient_alias, tuple(unbound), frozenset(projected_unbound), frozenset(grouped))


def _node_properties(schema: SchemaCards) -> set[str]:
    """The sphn properties whose object is a node of the record (a linked record, a sub-node, a code
    node), not a literal, a value-set member or a provenance node shared by every record of the patient.
    Without cards the set is empty and only the code properties (has*Code) are known to the caller."""
    found = set()
    for card in schema.classes.values():
        for prop, info in card["properties"].items():
            if info.get("kind") != "resource":
                continue
            targets = info.get("targets") or ([info["target"]] if info.get("target") else [])
            if info.get("code_system") or any(target not in PROVENANCE_CLASSES for target in targets):
                found.add(prop)
    return found


def _record_hint(asked: _Asked, counts: _Counts, algebra, text: str, schema: SchemaCards) -> str:
    alias = re.sub(r"[^a-z0-9]+", "_", asked.phrase)
    typed = _typed_variables(algebra, text)
    record = _record_for(asked, typed, schema)
    name = f"?{record}" if record is not None else "?record"
    where = "" if record is not None else " where ?record is the variable typed with the records' class" + (
        f" (one of {', '.join('?' + str(variable) for variable, _ in typed)})" if 1 < len(typed) <= 4 else ""
    )
    counted = f"(COUNT(DISTINCT {name}) AS ?{alias})"
    asks = f"The question asks how many {asked.phrase}, but"
    if Variable(alias) in counts.projected_unbound:  # SELECT ?stays ... GROUP BY ?stays, bound nowhere
        clauses = " and GROUP BY" if Variable(alias) in counts.grouped else ""
        return f"{asks} nothing binds ?{alias}: replace it in the SELECT{clauses} with {counted}{where}."
    if counts.unbound:  # COUNT(DISTINCT ?x) with nothing binding ?x: an attempt at the record count
        first = counts.unbound[0]
        listed = " or ".join(f"?{variable}" for variable in counts.unbound[:2])
        return (
            f"{asks} nothing binds {listed}, so the COUNT over it is empty. Bind the record, not the patient, "
            f"inside the block that selects it: BIND({name} AS ?{first}){where}, and keep COUNT(DISTINCT ?{first})."
        )
    if counts.patients is None:
        return f"{asks} the query never counts them. Add {counted} to the SELECT{where}."
    patients = counts.patients
    if str(patients) == alias or _mentions(_alias_words(patients), RECORD_WORDS):
        patients = Variable("patients")  # the patient count sits under the records' name
    return f"{asks} the query counts only patients. Add {counted} next to (COUNT(DISTINCT ?g) AS ?{patients}){where}."


def _typed_variables(algebra, text: str) -> list[tuple[Variable, str]]:
    """(variable, class) for every '?x a sphn:Class' pattern, in the order of the text."""
    classes: dict[Variable, str] = {}
    for subject, predicate, obj in _triples(algebra):
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(obj, URIRef) and str(obj).startswith(SPHN):
            classes.setdefault(subject, str(obj)[len(SPHN):])
    return sorted(classes.items(), key=lambda item: (text.find(f"?{item[0]}"), str(item[0])))


def _record_for(asked: _Asked, typed: list[tuple[Variable, str]], schema: SchemaCards) -> Variable | None:
    """The query's variable for the records: the typed variable whose class the question's words name best
    (?procedure a sphn:ImagingProcedure for "imaging procedures", ?case a sphn:AdministrativeCase for
    "stays"; on a tie a record class, then the shorter name: Death before DeathDate), or the only typed
    variable."""
    if len(typed) == 1:
        return typed[0][0]
    named = {cls for phrases, classes in SYNONYMS.items() for phrase in phrases for cls in classes
             if " " not in phrase and stem(phrase) in asked.stems}
    named |= {cls for noun, classes in NOUN_CLASSES.items() for cls in classes if noun in asked.stems}
    scored = []
    for variable, cls in typed:
        parts = words(cls)
        matched = len(asked.stems & {stem(part) for part in parts}) + (1 if cls in named else 0)
        if matched:
            marked = RECORD_MARKER in schema.classes.get(cls, {}).get("properties", {})
            scored.append(((matched, marked, -len(parts)), variable))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    candidates = [variable for score, variable in scored if score == best]
    return candidates[0] if len(candidates) == 1 else None


CHECKS = [check_question_coverage, check_record_count_asked]
