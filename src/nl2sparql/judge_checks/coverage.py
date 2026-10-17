"""Judge check: the query restricts to the coded concepts the question names.

The console showed "How many patients have cholera on a hospital bill, by sex?" answered with all
patients by sex: the query agent dropped the cholera pattern, and no other check compares the codes the
question names with the query. The terminology index (context/terminology.py) maps the question's words
to the code candidates of the prompt; this check takes the candidates the question names strongly and
requires the query to use that code, a subcode or a parent code of the same system anywhere: a typed
pattern, VALUES, a FILTER, or a string that spells the code or its name.

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
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rdflib import Literal, URIRef

from nl2sparql.context.bm25 import STOPWORDS, TOKEN_RE, fold
from nl2sparql.context.schema import SYNONYMS, SchemaCards, stem, words
from nl2sparql.context.terminology import ICD_CODE_RE, SEMANTIC_TAG_RE, TerminologyIndex, label_words, parent_code
from nl2sparql.models import CodeEntry
from nl2sparql.sparql.parse import ParsedQuery
from nl2sparql.sparql.prefixes import INFERENCE_RULE_SET, STANDARD_PREFIXES
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
