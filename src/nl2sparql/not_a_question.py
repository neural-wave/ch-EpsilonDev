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
import unicodedata

from rdflib import URIRef
from rdflib.plugins.sparql.parserutils import CompValue

from nl2sparql.sparql.parse import parse_sparql

NOT_A_QUESTION_REASON = "This is not a question about the patient data."

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "´": "'", "`": "'"})
_EMOTICON = re.compile(r"(?<!\w)(?:[:;=][-'^o]?[dpos3]|x[dp])(?!\w)|(?<!\w)[ot^>-]_+[ot^<-](?!\w)", re.I)

# Whole messages made of these phrases are small talk. Never add a phrase that is also a clinical term or
# abbreviation: a bare "k" is potassium ("K+?"), "all" after a greeting is a leukaemia ("Hi, ALL?").
_GREETING = (r"(?:hi+|hey+|hello+|helo|hallo|hullo|hiya|heya|heyo|hai|howdy|hola|ola|yo|greetings|"
             r"good (?:morning|afternoon|evening|day|night)|morning|"
             r"gruezi|gruessech|hoi|salu|salut|servus|moin|guten (?:morgen|tag|abend)|bonjour|bonsoir|coucou|"
             r"buongiorno|buonasera|salve|buenos dias|buenas(?: tardes| noches)?|buen dia|"
             r"what's up|whats up|whatsup|wassup|wazzup|sup)")
_ADDRESSEE = (r"(?: (?:there|everyone|everybody|team|guys|folks|bot|assistant|friend|world|again|you|y'all|"
              r"people|mate|buddy|dude|sir|human|ai|robot|zusammen|mitenand|a tous|a todos|equipo|a tutti)){0,2}")
_THANKS = (r"(?:thanks?(?: a lot| so much| a bunch| a million| in advance| again)?|thanks+|thank (?:you|u)(?: (?:so|very) much)?|"
           r"thank youu+|thx|thnx|thanx|ty|tysm|tyvm|many thanks|much appreciated|(?:i )?appreciate it|cheers|"
           r"thanks? (?:you )?for (?:your|the) help|"
           r"merci(?: beaucoup| bien| vielmal)?|danke(?: schon| sehr| vielmal)?|dankeschon|vielen dank|"
           r"grazie(?: mille)?|(?:muchas )?gracias|obrigad[oa])")
_BYE = (r"(?:bye+|goodbye|good bye|see (?:you|ya)(?: later| soon| tomorrow)?|cya|ttyl|take care|bye for now|"
        r"have a (?:nice|good|great) (?:day|one|evening|weekend)|that's all(?: for now)?|i'm done|nothing else|"
        r"ciao|adios|chao|chau|tschuss|tschau|auf wiedersehen|au revoir|a bientot|arrivederci|hasta luego|"
        r"hasta pronto|nos vemos|bonne journee|bonne soiree)")
_ACK = (r"(?:(?:ok+|okay+|alright) then|ok+|okay+|okey|oki|okie(?: dokie)?|o k|cool|great|nice|perfect|awesome|"
        r"yes|yep|yup|yeah|yea|no|nope|nah|sure|alright|all right|alrighty|l+o+l+(?:o+l+)*|lmao|rofl|ha(?:ha)+|"
        r"he(?:he)+|got it|gotcha|understood|noted|i see|makes sense|fair enough|sounds good|no worries|"
        r"you're welcome|wow|amazing|excellent|interesting|fine|very good|great job|good job|well done|"
        r"nice work|good bot|h+m+|u+m+|huh|oh|ah|oops|sorry|my bad|never ?mind|nvm|whatever|idk|"
        r"nice to meet you|pleased to meet you|"
        r"that(?:'s| is| was) (?:very )?(?:helpful|great|perfect|nice|cool|awesome|quick|fast|amazing)|"
        r"i'm (?:fine|good|ok|okay|great|well)|and you|nein|oui|non|genau|alles klar|super|d'accord|parfait|"
        r"perfetto|perfecto|genial|vale|va bene|de nada|de rien|prego)")
_META = (r"(?:i have a question|quick question|can i ask (?:you )?(?:something|a question)|how do you work|"
         r"help(?: me| please)?|please help|i need help|can you help(?: me)?|"
         r"are you there|(?:is )?any(?:one|body) there|you there|is (?:this|it) working|are you working|"
         r"can you hear me|who are you|what are you|what can you do|what can i ask(?: you)?|"
         r"what can you help (?:me )?with|what do you do|how does (?:this|it) work|"
         r"how (?:are|r) (?:you|u)(?: doing| feeling)?(?: today)?|how u doin|hru|how're you|how do you do|"
         r"how have you been|how is your day(?: going)?|how's your day|how's life|how are things|what's new|"
         r"how is it going|how's it going|how (?:old|tall) are you|how much do you weigh|"
         r"are you (?:alive|sick|ill|human|real|online|awake|a bot|a robot|an ai)|"
         r"are you (?:feeling )?(?:ok|okay|alright|well)|what(?:'s| is) your (?:name|gender|age)|"
         r"where are you from|where were you born|when is your birthday|when were you born|"
         r"who (?:made|built|created) you|wie geht's|wie geht es dir|ca va|comment ca va|come stai|que tal|"
         r"como estas|tudo bem)")
# Only as the whole message: "test" next to anything else may be a lab test ("no test?", "HI test?").
_ALONE = r"(?:test(?:ing)?(?: \d{1,3})*|this is a test|just testing|test message|mic check|is this thing on|ping)"
_PHRASE = rf"(?:(?:{_GREETING}|{_THANKS}|{_BYE}){_ADDRESSEE}|{_ACK}|{_META})"
_SMALL_TALK = re.compile(rf"^(?:{_ALONE}|{_PHRASE}(?: {_PHRASE})*)$")
_LETTER = re.compile(r"[^\W\d_]")
_MAX_WORDS = 12  # small talk is short; the cap also bounds the regex's backtracking


def is_small_talk(text: str) -> bool:
    """True for a message made only of small-talk phrases, or of punctuation and emoji alone. A message with
    digits and no letter ("2020?", "65+", a bare code) goes to the model."""
    text = _EMOTICON.sub(" ", text)
    if not _LETTER.search(text):
        return not re.search(r"\d", text)
    folded = unicodedata.normalize("NFKD", text.translate(_APOSTROPHES).lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    words = re.sub(r"[^\w\s']|_", " ", folded)
    words = " ".join(re.sub(r"\B'|'\B", " ", words).split())
    return len(words.split()) <= _MAX_WORDS and bool(_SMALL_TALK.match(words))


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
