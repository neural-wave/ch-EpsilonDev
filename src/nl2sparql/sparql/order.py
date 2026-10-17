"""Put the most selective triple patterns first, the way the organizers' own queries do.

On 100,000 patients, the same query took 0.01 s written code-first with `DEFINE sql:select-option
"order"` (the official worked example) and 1.4 s written record-first: Virtuoso joined a broad class
pattern before the typed code pattern. Models do not order patterns reliably, so this pass does it in
code: inside the first `GRAPH ?g { ... }` block it moves the typed code patterns (`?c a icd-10-gm:E11
OPTION (...)`) to the front, then the statements that use those code variables (`?x sphn:hasCode ?c`),
then everything else in its original order. When the block then starts with a code pattern, the DEFINE
line asks Virtuoso to keep the written order. The rewrite is syntactic and keeps the query's meaning;
anything it does not understand is returned unchanged.
"""
from __future__ import annotations

import re

DEFINE_ORDER = 'DEFINE sql:select-option "order"'
GRAPH_BLOCK_RE = re.compile(r"GRAPH\s+\?\w+\s*\{", re.IGNORECASE)
CODE_STATEMENT_RE = re.compile(
    r"^\?(\w+)\s+(?:a|rdf:type)\s+(?:icd-10-gm|chop|snomed|loinc|atc|ucum):[^\s;]+", re.IGNORECASE)
BLOCK_KEYWORDS = ("OPTIONAL", "MINUS", "FILTER", "BIND", "VALUES", "GRAPH", "SERVICE", "{", "SELECT")


def _split_statements(body: str) -> list[str] | None:
    """Top-level statements of a group pattern, each with its terminator. None when unbalanced."""
    statements, buffer, depth, i = [], [], 0, 0
    quote: str | None = None
    while i < len(body):
        ch = body[i]
        buffer.append(ch)
        if quote:
            if ch == quote and body[i - 1] != "\\":
                quote = None
        elif ch in ('"', "'", "<"):
            if ch == "<" and (i + 1 >= len(body) or body[i + 1] in " =<\t\n"):
                pass  # a comparison, not an IRI
            else:
                quote = ">" if ch == "<" else ch
        elif ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
            if depth < 0:
                return None
            text = "".join(buffer).lstrip().upper()
            # A block ("OPTIONAL { ... }") or a FILTER/BIND ends a statement without a "."
            if depth == 0 and (ch == "}" or text.startswith(("FILTER", "BIND"))):
                j = i + 1
                while j < len(body) and body[j] in " \t":
                    j += 1
                if j < len(body) and body[j] == ".":
                    buffer.append(".")
                    i = j
                statements.append("".join(buffer))
                buffer = []
        elif ch == "." and depth == 0:
            next_char = body[i + 1] if i + 1 < len(body) else " "
            if next_char.isspace() or i + 1 >= len(body):
                statements.append("".join(buffer))
                buffer = []
        i += 1
    if depth != 0 or quote:
        return None
    rest = "".join(buffer)
    if rest.strip():
        statements.append(rest)
    return statements


def _kind(statement: str, code_vars: set[str]) -> int:
    text = statement.strip()
    if CODE_STATEMENT_RE.match(text):
        return 0
    if text.upper().startswith(BLOCK_KEYWORDS):
        return 2
    if any(re.search(rf"\?{re.escape(var)}\b", text) for var in code_vars):
        return 1
    return 2


def reorder(sparql: str) -> str:
    match = GRAPH_BLOCK_RE.search(sparql)
    if not match or DEFINE_ORDER in sparql:
        return sparql
    start = match.end()
    depth, end, quote = 1, None, None
    for i in range(start, len(sparql)):
        ch = sparql[i]
        if quote:
            if ch == quote and sparql[i - 1] != "\\":
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "<" and i + 1 < len(sparql) and sparql[i + 1] not in " =<\t\n":
            quote = ">"  # an IRI, not a comparison
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return sparql
    body = sparql[start:end]
    statements = _split_statements(body)
    if not statements:
        return sparql
    code_vars = {m.group(1) for s in statements if (m := CODE_STATEMENT_RE.match(s.strip()))}
    if not code_vars or len(statements) < 2:
        return sparql
    kinds = [_kind(s, code_vars) for s in statements]
    if kinds[0] == 0 and kinds == sorted(kinds):
        return DEFINE_ORDER + "\n" + sparql.lstrip("\n")  # already code-first: only lock the order
    ordered = [s for k in (0, 1, 2) for s, kind in zip(statements, kinds) if kind == k]
    indent = re.match(r"[ \t]*", statements[0].lstrip("\n")).group(0)
    new_body = ("\n" + indent).join(s.strip() for s in ordered)
    leading = body[: len(body) - len(body.lstrip())]
    trailing = body[len(body.rstrip()):]
    rewritten = sparql[:start] + leading + new_body + trailing + sparql[end:]
    return DEFINE_ORDER + "\n" + rewritten.lstrip("\n")
