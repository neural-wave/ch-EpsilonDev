"""Requests that are not questions about the patient data.

Two signals, one answer (no query, not compliant, NOT_A_QUESTION_REASON):
- small talk, caught in code before any model call: greetings, thanks, "how are you", "ok", "test", text
  without a single letter. Only whole messages made of such phrases: "hello, how many patients have
  cholera?" is a question.
- the model's own verdict: asked something it cannot translate ("what is the weather in Zurich?"), the
  query agent writes a query that reads no data and names no term of the schema or its terminologies, only
  BIND or VALUES of plain text. Every patient-data question reads at least one triple pattern, and a schema
  question ("what properties does the lab test result class have?", allowed: GUARD.md section 6) names
  schema terms, so neither is caught.

Without this, a guard retry turned "hey" into "count every patient" (17 October).
"""
from __future__ import annotations

import re

from rdflib import URIRef
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.sparql.parse import parse_sparql

NOT_A_QUESTION_REASON = "This is not a question about the patient data."

_GREETING = r"(?:hi+|hey+|hello+|hallo|hiya|howdy|hola|yo|greetings|good (?:morning|afternoon|evening|day|night))"
_ADDRESSEE = r"(?: (?:there|all|everyone|everybody|team|guys|folks|bot|assistant|friend))?"
_THANKS = r"(?:thanks?(?: a lot)?|thank you(?: (?:so|very) much)?|thx|ty|cheers|merci|gracias)"
_BYE = r"(?:bye|goodbye|good bye|see (?:you|ya)(?: later| soon)?|ciao|adios)"
_ACK = r"(?:ok(?:ay)?|k|cool|great|nice|perfect|awesome|yes|yep|yeah|no|nope|sure|alright|lol|ha(?:ha)+)"
_META = (r"(?:test(?:ing)?|ping|help|are you there|who are you|what are you|what can you do|"
         r"how are you(?: doing)?(?: today)?|how is it going|how's it going|what's up|whats up|sup)")
_PHRASE = rf"(?:{_GREETING}{_ADDRESSEE}|{_THANKS}|{_BYE}|{_ACK}|{_META})"
_SMALL_TALK = re.compile(rf"^{_PHRASE}(?: {_PHRASE})*$")
_LETTER = re.compile(r"[^\W\d_]")


def is_small_talk(text: str) -> bool:
    """True for a message made only of small-talk phrases, or without a single letter."""
    if not _LETTER.search(text):
        return True
    words = " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())
    return bool(_SMALL_TALK.match(words))


def reads_no_data(sparql: str) -> bool:
    """True when the query parses, has no triple pattern anywhere and names no schema or terminology term
    (no http IRI): it reads no data and talks about none. An unparseable query is the judge's business."""
    parsed = parse_sparql(sparql)
    if parsed.algebra is None:
        return False
    return not _has_triples(parsed.algebra) and not _names_a_term(parsed.algebra)


def _names_a_term(node) -> bool:
    if isinstance(node, URIRef):
        return str(node).startswith(("http://", "https://"))
    if isinstance(node, (list, tuple, set, frozenset)):
        return any(_names_a_term(item) for item in node)
    if isinstance(node, dict):  # CompValue is a dict; VALUES rows are dicts of variable -> term
        return any(_names_a_term(value) for key, value in node.items() if not str(key).startswith("_"))
    return False


def _has_triples(node) -> bool:
    if isinstance(node, (list, tuple)):
        return any(_has_triples(item) for item in node)
    if isinstance(node, CompValue):
        if node.name == "BGP" and node.get("triples"):
            return True
        return any(_has_triples(value) for key, value in node.items() if not key.startswith("_"))
    return False
