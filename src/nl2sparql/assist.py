"""Demo helper outside the judged API: "Did you mean...?" choices for a vague question.

Plain code: no model call, no database. It only proposes new question texts; whatever the user
picks goes through the whole pipeline, so every choice is checked by the guard like any other
request. Nothing here is reachable from POST /nl-to-sparql.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from nl2sparql.context.terminology import ICD_CODE_RE, TerminologyIndex

BLOCK_RE = re.compile(r"^([A-Z])(\d{2})-([A-Z])(\d{2})$")  # a classification block such as E10-E14
COUNT_RE = re.compile(r"\b(how many|how often|number of|count)\b", re.IGNORECASE)
HOW_MANY_RE = re.compile(r"^how many\s+(.+)$", re.IGNORECASE)
DID_YOU_MEAN = "Did you mean…"
UNIT_RE = re.compile(
    r"\b(patients?|people|persons?|individuals?|men|women|children|babies|adults|stays?|admissions?|"
    r"cases?|hospitali[sz]ations?|records?|events?|tests?|results?|diagnos[ie]s|procedures?|"
    r"prescriptions?|administrations?|births?|deaths?|measurements?|samples?)\b",
    re.IGNORECASE,
)
MAX_CHOICES = 5
MAX_LABEL_CHARS = 60
WORD_RE = re.compile(r"\w+")
BRACKETS_RE = re.compile(r"\s*\[[^\]]*\]")


@dataclass(frozen=True)
class Choice:
    label: str  # what the button says
    question: str  # the full question it sends


@dataclass(frozen=True)
class Clarification:
    prompt: str
    choices: list[Choice]

    def to_json(self) -> dict:
        return {"prompt": self.prompt, "choices": [asdict(choice) for choice in self.choices]}


def _base(text: str) -> str:
    return text.strip().rstrip("?.! ")


def _words_as_written(text: str) -> set[str]:
    return set(WORD_RE.findall(text.lower()))


def _readable_label(labels: tuple[str, ...], question: str) -> str:
    """The label sharing most words with the question, compared as written: the index has German and
    French labels side by side, and folding accents would let "diabètes" tie with "diabetes". Notes in
    brackets are dropped and long labels shortened, since the label goes on a button."""
    words = _words_as_written(question)
    label = max(labels, key=lambda label: len(_words_as_written(label) & words), default="")
    label = BRACKETS_RE.sub("", label).strip(" ,")
    return label if len(label) <= MAX_LABEL_CHARS else label[:MAX_LABEL_CHARS - 1].rstrip(" ,") + "…"


def _block_children(index: TerminologyIndex, block: str) -> list[str]:
    match = BLOCK_RE.match(block)
    if not match or match.group(1) != match.group(3):
        return []
    letter, first, last = match.group(1), int(match.group(2)), int(match.group(4))
    return sorted(
        code for code in index.by_code
        if len(code) == 3 and code[0] == letter and code[1:].isdigit() and first <= int(code[1:]) <= last
    )


def clarify(text: str, index: TerminologyIndex) -> Clarification | None:
    """Choices for a vague question, or None when the question is specific enough to answer."""
    base = _base(text)
    if not ICD_CODE_RE.search(text):  # a question naming a code is already specific
        candidates = index.candidates(text, 5)
        block = next((entry for entry in candidates if BLOCK_RE.match(entry.code)), None)
        children = _block_children(index, block.code) if block else []
        # "type 2 diabetes" also matches E11 inside the block: the question already says which kind.
        named = any(entry.code.split(".")[0] in children for entry in candidates)
        if len(children) >= 2 and not named:
            name = _readable_label(block.labels, text)
            choices = []
            for code in children[:MAX_CHOICES - 1]:
                label = _readable_label(index.by_code[code].labels, text)
                # The code alone carries the meaning: the index finds it and gives the model its label.
                choices.append(Choice(f"{label} ({code})", f"{base}, specifically ICD-10-GM {code}?"))
            choices.append(Choice(f"Any kind of {name}", f"{base}?"))
            return Clarification(f"Which kind of {name}?", choices)
    if COUNT_RE.search(text) and not UNIT_RE.search(text):
        how_many = HOW_MANY_RE.match(base)
        if how_many:  # "How many have cholera" -> "How many patients have cholera?"
            rest = how_many.group(1)
            questions = [f"How many patients {rest}?", f"How many hospital stays were there for patients who {rest}?"]
        else:
            questions = [f"{base}, counting patients?", f"{base}, counting hospital stays?"]
        return Clarification(DID_YOU_MEAN, [Choice(question, question) for question in questions])
    return None

