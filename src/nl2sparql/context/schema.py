"""Schema cards: the SPHN classes that really exist in the data, with their properties.

A card says, per property, what the data holds: a value and its datatype, a value set (listed in
full, sphn-individual entries are not restricted), a link and every class it reaches, or a code node
and the terminology its codes come from (ICD-10-GM, CHOP, SNOMED CT or only the generic sphn:Code).
A coded property shows no example code: the model copied the placeholder `snomed:<code>` as it stood
(11 dev answers of the 17 October run), so the card names the pattern `?c a ?concept` and points at the
code candidates for a concrete code. The link hasSubjectPseudoIdentifier, which every record carries,
is shown only on the SubjectPseudoIdentifier card: one patient is one graph, no compliant reference
joins through it, and the join misled the model three times.

Optional value sets (assets/value_sets.json, built by scripts/build_value_sets.py) add the labels of
the few coded values a property takes in the data, e.g. the six SNOMED CT concepts of
CivilStatus.hasCode. They derive from restricted terminologies, so they live in assets/ and are never
committed."""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from pathlib import Path

from nl2sparql.context.bm25 import normalize_text, tokenize
from nl2sparql.models import CodeEntry
from nl2sparql.sparql.prefixes import STANDARD_PREFIXES

CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
SPHN_TERM_RE = re.compile(r"sphn:([A-Za-z]+)")
KIND_LABELS = {"literal": "value", "individual": "value set", "resource": "link", "mixed": "mixed", "unknown": "?"}

# The prefix of each terminology a card can name; any other code system is shown as its namespace.
CODE_SYSTEM_PREFIXES = {
    "ICD-10-GM": "icd-10-gm", "CHOP": "chop", "SNOMED CT": "snomed", "LOINC": "loinc", "UCUM": "ucum",
    "ATC": "atc", "GENO": "geno", "ORDO": "ordo",
}
# Prefixes the query prompt and the official examples already use; a card that uses any other one
# prints its namespace, so the model declares it right.
PROMPT_PREFIXES = frozenset({"sphn", "sphn-individual", "icd-10-gm", "chop", "snomed"})
# Classes every record links to; following a link there never helps a question, and an example that
# uses one (official-15 lists identifiers) does not make the question need it.
PROVENANCE_CLASSES = frozenset({"SourceSystem", "DataProvider", "SubjectPseudoIdentifier"})
# The link from every record to the patient's pseudo-identifier: rendered only on the card of the class
# it reaches, where a question that names identifiers finds the vocabulary for a literal translation.
PATIENT_LINK = "hasSubjectPseudoIdentifier"
# How a card tells the model to match a coded property, with no code it could copy verbatim.
CONCEPT_PATTERN = "?c a ?concept; one code from the candidates to match a concept"
MAX_TARGETS = 12  # Interpretation.hasInput reaches 79 classes; a card lists the most frequent ones
# Name parts shared by many classes or properties, or that most questions contain ("number",
# "average"), weigh half: "medical" alone must not bring MedicalDevice. The loaded cards add every
# part that GENERIC_PART_NAMES names or more share.
GENERIC_WORDS = frozenset({"medical", "event", "procedure", "date", "code", "data", "result", "test", "lab",
                           "measurement", "time", "name", "type", "status", "value", "number", "average", "total",
                           "first", "last", "year", "month", "day", "hour", "minute", "second", "count", "mode",
                           "unit", "start", "end"})
GENERIC_PART_NAMES = 4
# Labels too common to point at a class ("Other" is a member of five value sets).
UNSPECIFIC_LABELS = frozenset({"other", "none", "not", "unknown", "general", "specific", "yes", "no", "applicable"})
IRREGULAR_PLURALS = {"diagnoses": "diagnosis", "analyses": "analysis", "men": "man", "women": "woman"}
# "give drugs", "drugs given", "medication we gave": the verb counts next to a drug noun only, since
# "Give the number of stays" is an imperative, not an administration.
GIVE_PHRASES = tuple(
    f"{first} {second}"
    for verb in ("give", "given", "gave", "giving")
    for noun in ("drug", "drugs", "medication", "medications", "medicine", "medicines", "dose", "doses")
    for first, second in ((verb, noun), (noun, verb))
)
# Everyday words for backbone classes whose names the questions never use. A phrase with a stopword
# ("in hospital", "by mouth") is matched as written, since the tokenizer would drop the stopword.
SYNONYMS = {
    ("stay", "stays", "admission", "admissions", "admitted", "discharge", "discharged", "discharges",
     "hospitalization", "hospitalisation", "hospitalized", "hospitalised", "in hospital", "inpatient",
     "inpatients"): ("AdministrativeCase",),
    ("citizen", "citizens", "citizenship", "nationality", "nationalities"): ("Nationality",),
    ("bmi",): ("BodyMassIndex",),
    ("blood pressure",): ("BloodPressure",),
    ("died", "dies", "die", "dying", "death", "deaths", "deceased", "mortality"): ("Death", "DeathDate"),
    ("cancer", "cancers", "tumour", "tumours", "tumor", "tumors", "oncology", "oncological"): ("OncologyDiagnosis",),
    ("ingredient", "ingredients", "substance", "substances"): ("Substance",),
    ("specialty", "specialties", "speciality", "specialities"): ("TherapeuticArea",),
    ("follow up", "followed up"): ("FollowUp", "VitalStatus"),
    ("born", "birth", "births", "baby", "babies", "newborn", "newborns", "delivered", "delivery"): ("Birth",),
    ("early", "preterm", "premature", "prematurely", "gestation", "gestational"): ("GestationalAgeAtBirth",),
    ("sex", "gender", "male", "female", "men", "women", "man", "woman"): ("AdministrativeSex",),
    # Drugs given, administered or taken by a route (official-07 asked by route and got no card).
    (*GIVE_PHRASES, "administer", "administered", "administering", "administration", "administrations",
     "route", "routes", "by mouth", "orally", "iv", "intravenous", "intravenously"): ("DrugAdministrationEvent",),
    # The purpose of a procedure (q-057 and q-074 asked for diagnostic and guidance intents).
    ("intent", "intents", "intention", "guidance", "guide", "guided", "guiding", "screening", "diagnostic",
     "diagnostics"): ("Intent",),
    # A request that names identifiers is forbidden, but its literal translation still needs the vocabulary.
    ("identifier", "identifiers", "pseudo identifier", "pseudo identifiers", "pseudonym", "pseudonyms",
     "pseudonymised", "pseudonymized", "patient id", "patient ids"): ("SubjectPseudoIdentifier",),
}


def words(name: str) -> list[str]:
    return [part.lower() for part in CAMEL_RE.findall(name)]


def stem(word: str) -> str:
    """A crude stem applied to both sides of a comparison: 'stays' and 'stay' meet at 'stay'."""
    word = IRREGULAR_PLURALS.get(word, word)
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    return word


def _label_key(label: str) -> frozenset[str]:
    """The stems a question must contain to hit a value label; empty when the label is unspecific."""
    stems = frozenset(stem(token) for token in tokenize(" ".join(words(label))) if not token.isdigit())
    if not stems or stems <= UNSPECIFIC_LABELS or (len(stems) == 1 and len(next(iter(stems))) < 4):
        return frozenset()
    return stems


class SchemaCards:
    def __init__(self, classes: dict[str, dict], schema_terms: list[str], value_sets: dict | None = None):
        self.classes = classes
        self.schema_terms = set(schema_terms)
        self.data_terms = set(classes) | {prop for card in classes.values() for prop in card["properties"]}
        # class -> property -> [{"value", "label", "count"}]: the loaded labels first, then the value
        # sets the cards carry themselves (sphn-individual entries, measured on the data).
        self.value_sets = {name: dict(props) for name, props in (value_sets or {}).items()}
        for name, card in classes.items():
            for prop, info in card["properties"].items():
                if info.get("values") and prop not in self.value_sets.get(name, {}):
                    self.value_sets.setdefault(name, {})[prop] = [
                        {"value": f"sphn-individual:{value}", "label": value, "count": count}
                        for value, count in info["values"].items()
                    ]
        self._class_parts = {name: words(name) for name in classes}
        self._property_parts = {
            name: {prop: [part for part in words(prop) if part != "has"] for prop in card["properties"]}
            for name, card in classes.items()
        }
        self._generic = set(GENERIC_WORDS)
        for names in (self._class_parts.values(), [p for props in self._property_parts.values() for p in props.values()]):
            shared = Counter(part for parts in names for part in set(parts))
            self._generic |= {part for part, n in shared.items() if n >= GENERIC_PART_NAMES}
        self._label_keys = {
            name: [key for prop in props.values() for key in (_label_key(v["label"]) for v in prop) if key]
            for name, props in self.value_sets.items() if name in classes
        }
        # Code IRI -> the classes whose value sets hold it: a code candidate that is a member of a set
        # names the class as surely as a value label does (the screening-intent code names Intent).
        # An unspecific member ("Other" sits in two sets) names nothing, as its label would not.
        self._classes_by_value: dict[str, set[str]] = {}
        for name, props in self.value_sets.items():
            for prop in props.values():
                for value in prop:
                    if name in classes and _label_key(value["label"]) and (iri := _expand(value["value"])):
                        self._classes_by_value.setdefault(iri, set()).add(name)
        self._patient_link_targets = {
            target for card in classes.values() for target in _targets(card["properties"].get(PATIENT_LINK, {}))
        }
        self._synonyms = {
            " ".join(stem(word) for word in phrase.split()): tuple(c for c in targets if c in classes)
            for phrases, targets in SYNONYMS.items() for phrase in phrases
        }

    @classmethod
    def load(cls, path: str | Path, value_sets_path: str | Path | None = None) -> "SchemaCards":
        path = Path(path)
        if not path.exists():
            return cls({}, [])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid schema cards file {path}: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid schema cards file {path}: root must be a JSON object")
        for field in ("classes", "schema_terms"):
            if field not in payload:
                raise ValueError(f"Invalid schema cards file {path}: missing required field {field!r}")
        classes, schema_terms = payload.get("classes", {}), payload.get("schema_terms", [])
        if not isinstance(classes, dict):
            raise ValueError(f"Invalid schema cards file {path}: 'classes' must be an object")
        if not isinstance(schema_terms, list) or any(not isinstance(term, str) for term in schema_terms):
            raise ValueError(f"Invalid schema cards file {path}: 'schema_terms' must be a list of strings")
        for name, card in classes.items():
            if not isinstance(name, str) or not isinstance(card, dict):
                raise ValueError(f"Invalid schema cards file {path}: each class must map a string name to an object")
            if not isinstance(card.get("instances"), int) or isinstance(card.get("instances"), bool):
                raise ValueError(f"Invalid schema cards file {path}: class {name!r} needs integer 'instances'")
            if not isinstance(card.get("patients"), int) or isinstance(card.get("patients"), bool):
                raise ValueError(f"Invalid schema cards file {path}: class {name!r} needs integer 'patients'")
            properties = card.get("properties")
            if not isinstance(properties, dict):
                raise ValueError(f"Invalid schema cards file {path}: class {name!r} needs object 'properties'")
            for prop, info in properties.items():
                if not isinstance(prop, str) or not isinstance(info, dict):
                    raise ValueError(f"Invalid schema cards file {path}: properties must map string names to objects")
                if "kind" in info and not isinstance(info["kind"], str):
                    raise ValueError(f"Invalid schema cards file {path}: property {prop!r} has invalid 'kind'")
                if "count" in info and not _is_int(info["count"]):
                    raise ValueError(f"Invalid schema cards file {path}: property {prop!r} has invalid 'count'")
                for field in ("target", "datatype", "code_system"):
                    if field in info and not isinstance(info[field], str):
                        raise ValueError(f"Invalid schema cards file {path}: property {prop!r} has invalid {field!r}")
                if "targets" in info and not (
                    isinstance(info["targets"], list) and all(isinstance(t, str) for t in info["targets"])
                ):
                    raise ValueError(f"Invalid schema cards file {path}: property {prop!r} has invalid 'targets'")
                for field in ("values", "code_systems"):
                    if field in info and not (
                        isinstance(info[field], dict)
                        and all(isinstance(k, str) and _is_int(v) for k, v in info[field].items())
                    ):
                        raise ValueError(f"Invalid schema cards file {path}: property {prop!r} has invalid {field!r}")
        return cls(classes, schema_terms, _load_value_sets(value_sets_path))

    @property
    def empty(self) -> bool:
        return not self.classes

    def in_data(self, term: str) -> bool:
        """True when the class or property occurs in the patient data (always True without cards)."""
        return self.empty or term in self.data_terms

    def closest(self, term: str) -> str | None:
        scored = [
            (difflib.SequenceMatcher(None, term, candidate).ratio(), candidate)
            for candidate in self.data_terms
        ]
        matches = [(score, candidate) for score, candidate in scored if score > 0.6]
        # Match get_close_matches' existing tie choice for the sorted candidate set.
        return max(matches, key=lambda item: (item[0], item[1]))[1] if matches else None

    def render(self, name: str) -> str:
        card = self.classes[name]
        parts, prefixes = [], set()
        for prop, info in card["properties"].items():
            values = self.value_sets.get(name, {}).get(prop)
            if prop == PATIENT_LINK:
                continue  # shown on the card it reaches, see PATIENT_LINK
            if values and all(v["value"].startswith("sphn-individual:") for v in values):
                parts.append(f"{prop} = one of " + " | ".join(v["value"] for v in values))
            elif values:  # code nodes: ?x sphn:<prop> ?c . ?c a <value>
                parts.append(f"{prop} -> code typed as one of " + " | ".join(f"{v['label']} {v['value']}" for v in values))
                prefixes.update(v["value"].split(":", 1)[0] for v in values if not v["value"].startswith("<"))
            elif info.get("code_system"):
                line, prefix = _code_line(prop, info)
                parts.append(line)
                if prefix:
                    prefixes.add(prefix)
            elif targets := _targets(info):
                shown = " | ".join(f"sphn:{target}" for target in targets[:MAX_TARGETS])
                more = f" (the {MAX_TARGETS} most frequent of {len(targets)} classes)" if len(targets) > MAX_TARGETS else ""
                parts.append(f"{prop} -> {shown}{more}")
            elif info.get("datatype"):
                parts.append(f"{prop} ({info['datatype']})")
            else:
                parts.append(f"{prop} ({KIND_LABELS.get(info.get('kind'), '?')})")
        namespaces = "".join(
            f" ({prefix}: <{STANDARD_PREFIXES[prefix]}>)"
            for prefix in sorted(prefixes - PROMPT_PREFIXES) if prefix in STANDARD_PREFIXES
        )
        reached = f" (every record links to it with {PATIENT_LINK})" if name in self._patient_link_targets else ""
        return f"sphn:{name} [{card['instances']} records, {card['patients']} patients]{reached}: " + "; ".join(parts) + namespaces

    def cards_for(self, text: str, k: int = 6, examples_sparql: str = "", codes: list[CodeEntry] | tuple = ()) -> list[str]:
        """Cards for the classes the question names (as sphn:Name, as words, as a synonym such as
        "stay" for AdministrativeCase, through a property name, a value label or a code candidate that
        is a member of the class's value set) first, then the classes the retrieved examples use. A
        shown card pulls the class behind a link the question points at (FollowUp.hasResult ->
        VitalStatus), never a provenance class, which only a question that names it brings in."""
        tokens = tokenize(text)
        if k <= 0 or self.empty or not tokens:
            return []
        question = _Question(tokens, text)
        named = {term for term in SPHN_TERM_RE.findall(text) if term in self.classes}
        used_by_examples = {
            term for term in SPHN_TERM_RE.findall(examples_sparql) if term in self.classes and term not in PROVENANCE_CLASSES
        }
        synonyms = {name for phrase, targets in self._synonyms.items() if question.has_phrase(phrase) for name in targets}
        # Classes whose value set holds a code candidate. A candidate can be a partial match ("Lost to
        # follow-up" for a follow-up question), so membership only makes the class worth pulling through
        # a link of a shown card (ImagingProcedure.hasIntent -> Intent for the screening-intent code).
        members = {name for entry in codes for name in self._classes_by_value.get(entry.iri, ())}

        def labelled(name: str) -> bool:
            return name not in PROVENANCE_CLASSES and any(key <= question.stems for key in self._label_keys.get(name, ()))

        def mentioned(name: str) -> bool:
            return (name in synonyms or name in members or labelled(name)
                    or self._ratio(self._class_parts[name], question)[0] >= 0.5)

        scored, pulled = [], {}
        for name in self.classes:
            name_ratio, matched = self._ratio(self._class_parts[name], question)
            properties = {prop: self._property_ratio(name, prop, question) for prop in self._property_parts[name]}
            score = (
                (1 + name_ratio if name in named or name_ratio >= 0.5 else 0)
                + (10 if name in named else 0)  # an explicit sphn:Name beats every other signal
                + (1.75 if name in synonyms else 0)
                + 0.75 * max(properties.values(), default=0.0)
                + (0.6 if labelled(name) else 0)
                + (0.5 if name in used_by_examples else 0)
            )
            if score <= 0:
                continue
            scored.append((score, len(matched), name))
            # Links worth following from this card: the question names the link itself or its target.
            pulled[name] = [
                target
                for prop, info in self.classes[name]["properties"].items()
                for target in _targets(info)
                if target in self.classes and target != name and target not in PROVENANCE_CLASSES
                and (properties[prop] > 0 or mentioned(target))
            ]
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))  # ties: more words, then name
        chosen: list[str] = []
        for _, _, name in scored:
            for candidate in [name, *pulled[name]]:
                if candidate not in chosen and len(chosen) < k:
                    chosen.append(candidate)
        # Spare slots go to the classes the best card links to: the model needs their properties to
        # use the link (Birth.hasGestationalAge -> GestationalAgeAtBirth.hasValue).
        for info in self.classes[chosen[0]]["properties"].values() if chosen else ():
            for target in _targets(info):
                if target in self.classes and target not in chosen and target not in PROVENANCE_CLASSES and len(chosen) < k:
                    chosen.append(target)
        return [self.render(name) for name in chosen]

    def _ratio(self, parts: list[str], question: "_Question") -> tuple[float, list[str]]:
        """Weighted share of a name's parts the question mentions, and the parts that matched. One
        generic part is not a match ("medical" alone is not MedicalDevice); a specific part, two
        parts or the whole name is ("billed" for BilledDiagnosis, "lab test" for LabTestEvent,
        "diagnosis" for Diagnosis)."""
        matched = [part for part in parts if question.mentions(part)]
        if not matched or (len(matched) == 1 < len(parts) and matched[0] in self._generic):
            return 0.0, []
        return sum(self._weight(part) for part in matched) / sum(self._weight(part) for part in parts), matched

    def _property_ratio(self, name: str, prop: str, question: "_Question") -> float:
        """A property counts when the question names most of it and a specific part of it, either
        two parts or one long word: "active ingredient", "outcome", but not "start" or "day"."""
        if name in PROVENANCE_CLASSES:
            return 0.0
        ratio, matched = self._ratio(self._property_parts[name][prop], question)
        if ratio < 0.5 or all(part in self._generic for part in matched) or (len(matched) < 2 and len(matched[0]) < 6):
            return 0.0
        return ratio

    def _weight(self, part: str) -> float:
        return 0.5 if part in self._generic else 1.0


class _Question:
    """The question as stems, bigrams and seven-letter prefixes, the forms a name part is matched in,
    and as folded text for the phrases the tokenizer would break up."""

    def __init__(self, tokens: list[str], text: str = ""):
        stems = [stem(token) for token in tokens]
        self.stems = set(stems)
        self.phrases = self.stems | {f"{a} {b}" for a, b in zip(stems, stems[1:])}
        self.prefixes = {token[:7] for token in tokens if len(token) >= 7}
        self.text = normalize_text(text)

    def mentions(self, part: str) -> bool:
        """'stays' mentions 'stay'; 'resuscitate' mentions 'resuscitation' (same first seven letters)."""
        return stem(part) in self.stems or (len(part) >= 7 and part[:7] in self.prefixes)

    def has_phrase(self, phrase: str) -> bool:
        """A synonym phrase: one stem or two in a row ('stays', 'blood pressure'), or, when it holds a
        stopword the tokenizer drops ('in hospital'), the phrase as written in the question."""
        return phrase in self.phrases or (" " in phrase and re.search(rf"\b{re.escape(phrase)}\b", self.text) is not None)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _targets(info: dict) -> list[str]:
    """Every class a link property reaches, most frequent first (the generic code type is not one)."""
    targets = info.get("targets") or ([info["target"]] if info.get("target") else [])
    return [target for target in targets if target != "Code"]


def _code_line(prop: str, info: dict) -> tuple[str, str | None]:
    """How to match the codes of a coded property, and the prefix the codes use. No example code: the
    prompt rules say how a concrete code is typed, with the inference option."""
    system = info["code_system"]
    if system == "generic":
        return f"{prop} -> generic sphn:Code (no terminology type: cannot be matched by ICD/CHOP/SNOMED)", None
    systems = info.get("code_systems") or {}
    total = sum(systems.values())
    share = f"{100 * systems[system] // total}%, the rest generic sphn:Code; " if len(systems) > 1 and total else ""
    prefix = CODE_SYSTEM_PREFIXES.get(system)
    label = f"{system} code" if prefix else f"code in <{system}>"
    return f"{prop} -> {label} ({share}{CONCEPT_PATTERN})", prefix


def _expand(value: str) -> str | None:
    """The full IRI of a value-set entry written as prefix:local or <iri>; None for an sphn-individual."""
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1]
    prefix, _, local = value.partition(":")
    if prefix == "sphn-individual" or prefix not in STANDARD_PREFIXES:
        return None
    return STANDARD_PREFIXES[prefix] + local


def _load_value_sets(path: str | Path | None) -> dict:
    """Coded values per class property (assets/value_sets.json); empty when the file is absent."""
    if path is None or not Path(path).exists():
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid value sets file {path}: {error}") from error
    classes = payload.get("classes") if isinstance(payload, dict) else None
    if not isinstance(classes, dict):
        raise ValueError(f"Invalid value sets file {path}: 'classes' must be an object")
    return classes
