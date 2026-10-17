"""Put the typed code pattern first, as the organizers' queries do, but only where that is the right plan.

On 100,000 patients the same query took 0.01 s written code-first with `DEFINE sql:select-option
"order"` (the official worked example) and 1.4 s written record-first: Virtuoso joined the broad class
pattern before the typed code pattern (spec-01, x70). Models do not order patterns reliably, so this
pass does it in code. The DEFINE makes Virtuoso join in the written order everywhere, also inside a
`;` chain, so a forced order is only as good as the whole written query: `?c a icd-10-gm:A00 ...
?dx a sphn:BilledDiagnosis ; sphn:hasRank ?r ; sphn:hasCode ?c` is a cross join of every A00 code
node with every billed diagnosis of the patient, with the `hasCode` link applied last (q-015 went
from 0.19 s to 9 s, q-017 from 1.3 s to 8.8 s). So the pass rewrites only a query whose plan it can
predict and returns every other query untouched, without the DEFINE:

1. exactly one typed code pattern (`?c a icd-10-gm:X`, `chop:X`, `snomed:X`, ... with or without the
   OPTION) in the whole query, a top-level statement of the first `GRAPH ?g { ... }` block, which also
   puts every `OPTION (inference ...)` there: none inside an OPTIONAL or anywhere else;
2. no UNION, MINUS or sub-select anywhere; OPTIONAL, FILTER, BIND and VALUES are allowed;
3. the statement that links to the code variable is rotated so that `?dx sphn:hasCode ?c` is its first
   triple (`?dx sphn:hasCode ?c ; a sphn:BilledDiagnosis ; sphn:hasRank ?r`), the worked-example order:
   one-row lookup per code node;
4. after the rotation every plain statement of the block shares a variable with the statements before
   it, so the forced order is a chain of joins and never a cross product.

The code pattern and the linking statements move to the front past a FILTER or a VALUES (their position
in a group has no meaning) and past a BIND or an OPTIONAL only when they share no variable with it, so
the algebra, and with it the meaning, is unchanged. The output stays valid for `parse_sparql`, and the
pass is idempotent: a query that already carries the DEFINE is returned as it is.
"""
from __future__ import annotations

import re

from nl2sparql.sparql.parse import INFERENCE_RE
from nl2sparql.sparql.prefixes import STANDARD_PREFIXES

DEFINE_ORDER = 'DEFINE sql:select-option "order"'
TERMINOLOGIES = ("icd-10-gm", "chop", "snomed", "loinc", "atc", "ucum")
_CODE_IRI = (r"(?:" + "|".join(map(re.escape, TERMINOLOGIES)) + r"):[^\s;,.]+(?:\.[^\s;,.]+)*"
             r"|<(?:" + "|".join(re.escape(STANDARD_PREFIXES[t]) for t in TERMINOLOGIES) + r")[^<>\s]+>")
CODE_ANYWHERE_RE = re.compile(r"\?(\w+)\s+(?:a|rdf:type)\s+(?:" + _CODE_IRI + r")", re.IGNORECASE)
CODE_STATEMENT_RE = re.compile(
    r"^\?(\w+)\s+(?:a|rdf:type)\s+(?:" + _CODE_IRI + r")\s*(?:OPTION\s*\([^()]*\))?\s*\.?$", re.IGNORECASE)
GRAPH_BLOCK_RE = re.compile(r"\bGRAPH\s+\?\w+\s*\{", re.IGNORECASE)
EXCLUDED_RE = re.compile(r"\bUNION\b|\bMINUS\b|\{\s*SELECT\b", re.IGNORECASE)
TAIL_RE = re.compile(r"^(?:\{|(?:OPTIONAL|MINUS|FILTER|BIND|VALUES|GRAPH|SERVICE|SELECT)\b)", re.IGNORECASE)
POSITION_FREE_RE = re.compile(r"^(?:FILTER|VALUES)\b", re.IGNORECASE)
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
IRI_RE = re.compile(r"<[^<>\"{}|^`\\\s]*>")
VARIABLE_RE = re.compile(r"[?$](\w+)")


def _blank(text: str, iris: bool) -> str:
    """Overwrite string literals (and IRIs) with spaces, so positions stay valid and their content is not read."""
    def spaces(match: re.Match) -> str:
        return " " * len(match.group(0))
    blanked = STRING_RE.sub(spaces, text)
    return IRI_RE.sub(spaces, blanked) if iris else blanked


def _split_statements(masked: str) -> list[tuple[int, int]] | None:
    """Spans of the top-level statements of a group pattern, each with its terminator. None when unbalanced.

    `masked` is the body with strings and IRIs blanked, so every brace, parenthesis and dot is structural.
    """
    spans, begin, depth, i = [], 0, 0, 0
    while i < len(masked):
        ch = masked[i]
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
            if depth < 0:
                return None
            head = masked[begin:i + 1].lstrip().upper()
            # A block ("OPTIONAL { ... }") or a FILTER/BIND ends a statement without a "."
            if depth == 0 and (ch == "}" or head.startswith(("FILTER", "BIND"))):
                j = i + 1
                while j < len(masked) and masked[j] in " \t":
                    j += 1
                if j < len(masked) and masked[j] == ".":
                    i = j
                spans.append((begin, i + 1))
                begin = i + 1
        elif ch == "." and depth == 0 and (i + 1 >= len(masked) or masked[i + 1].isspace()):
            spans.append((begin, i + 1))
            begin = i + 1
        i += 1
    if depth != 0:
        return None
    if masked[begin:].strip():
        spans.append((begin, len(masked)))
    return spans


def _strip(text: str, start: int, end: int) -> tuple[int, int]:
    """The span without surrounding whitespace, judged on the original text (a blanked string is not blank)."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _rotate(statement: str, masked: str, code_var: str, indent: str) -> str | None:
    """The statement with its `?x sphn:hasCode ?code` pair first. None when the `;` chain cannot be read.

    `masked` is the statement with strings and IRIs blanked; every span is cut on it and read on the original.
    """
    begin, end = _strip(statement, 0, len(statement))
    terminated = end > begin and statement[end - 1] == "."
    if terminated:
        end -= 1
    cuts, depth = [], 0
    for i in range(begin, end):
        if masked[i] in "([{":
            depth += 1
        elif masked[i] in ")]}":
            depth -= 1
        elif masked[i] == ";" and depth == 0:
            cuts.append(i)
    bounds = [(begin, cuts[0] if cuts else end)] + [(c + 1, n) for c, n in zip(cuts, cuts[1:] + [end])]
    parts = [_strip(statement, a, b) for a, b in bounds]
    parts = [(a, b) for a, b in parts if b > a]
    if not parts:
        return None
    subject = re.match(r"(\?\w+)\s+", masked[parts[0][0]:parts[0][1]])
    if not subject:
        return None
    first = parts[0][0] + subject.end()
    pairs = [(first, parts[0][1])] + parts[1:]
    link = None
    for index, (a, b) in enumerate(pairs):
        predicate = re.match(r"\S+\s+", masked[a:b])
        objects = masked[a + predicate.end():b] if predicate else ""
        if re.search(rf"\?{re.escape(code_var)}\b", objects):
            link = index
            break
    if not link:
        return statement.strip()  # the link is already first, or the code variable is this statement's subject
    ordered = [pairs[link]] + pairs[:link] + pairs[link + 1:]
    continuation = " ;\n" + indent + " " * (len(subject.group(1)) + 1)
    return (subject.group(1) + " " + continuation.join(statement[a:b] for a, b in ordered)
            + (" ." if terminated else ""))


def _block(masked: str) -> tuple[int, int] | None:
    """Start and end of the body of the first `GRAPH ?g { ... }` block, when it is a direct child of WHERE."""
    match = GRAPH_BLOCK_RE.search(masked)
    if not match or masked[:match.start()].count("{") - masked[:match.start()].count("}") != 1:
        return None
    depth = 1
    for end in range(match.end(), len(masked)):
        depth += {"{": 1, "}": -1}.get(masked[end], 0)
        if depth == 0:
            return match.end(), end
    return None


def reorder(sparql: str) -> str:
    if DEFINE_ORDER in sparql:
        return sparql
    masked = _blank(sparql, iris=True)
    without_strings = _blank(sparql, iris=False)
    if masked.count("{") != masked.count("}") or masked.count("(") != masked.count(")"):
        return sparql
    if EXCLUDED_RE.search(masked) or len(CODE_ANYWHERE_RE.findall(without_strings)) != 1:
        return sparql
    block = _block(masked)
    if block is None:
        return sparql
    start, end = block
    spans = _split_statements(masked[start:end])
    if not spans or len(spans) < 2:
        return sparql
    statements = [sparql[start + a:start + b] for a, b in spans]
    masked_statements = [masked[start + a:start + b] for a, b in spans]
    codes = [(i, CODE_STATEMENT_RE.match(without_strings[start + a:start + b].strip())) for i, (a, b) in enumerate(spans)]
    codes = [(i, m) for i, m in codes if m]
    if len(codes) != 1:
        return sparql
    code_index, code_var = codes[0][0], codes[0][1].group(1)
    if len(INFERENCE_RE.findall(sparql)) != len(INFERENCE_RE.findall(statements[code_index])):
        return sparql
    kinds = []
    for i, text in enumerate(masked_statements):
        head = text.strip()
        if i == code_index:
            kinds.append("code")
        elif TAIL_RE.match(head):
            kinds.append("tail")
        elif re.search(rf"\?{re.escape(code_var)}\b", head):
            kinds.append("link")
        else:
            kinds.append("plain")
    variables = [set(VARIABLE_RE.findall(text)) for text in masked_statements]
    moved = [code_index] + [i for i, kind in enumerate(kinds) if kind == "link"]
    for i in moved:
        for j in range(i):
            if kinds[j] == "tail" and not POSITION_FREE_RE.match(masked_statements[j].strip()) and variables[i] & variables[j]:
                return sparql  # moving past this BIND or OPTIONAL would change the meaning
    order = moved + [i for i, kind in enumerate(kinds) if kind in ("plain", "tail")]
    seen: set[str] = set()
    for i in order:
        if kinds[i] == "tail":
            continue
        if seen and not (variables[i] & seen):
            return sparql  # a statement that joins nothing before it: the forced order would be a cross product
        seen |= variables[i]
    indent = re.match(r"[ \t]*", statements[0].lstrip("\n")).group(0)
    rendered = []
    for i in order:
        text = statements[i].strip()
        if kinds[i] == "link":
            text = _rotate(statements[i], masked_statements[i], code_var, indent)
            if text is None:
                return sparql
        if kinds[i] != "tail" and not text.endswith("."):
            text += " ."  # a triple statement that was last in the block may not be last any more
        rendered.append(text)
    if rendered == [s.strip() for s in statements]:
        return DEFINE_ORDER + "\n" + sparql.lstrip("\n")  # already in the right order: only lock it
    body = sparql[start:end]
    leading = body[: len(body) - len(body.lstrip())]
    trailing = body[len(body.rstrip()):]
    rewritten = sparql[:start] + leading + ("\n" + indent).join(rendered) + trailing + sparql[end:]
    return DEFINE_ORDER + "\n" + rewritten.lstrip("\n")
