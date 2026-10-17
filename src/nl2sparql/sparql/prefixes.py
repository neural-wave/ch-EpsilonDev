"""Standard prefixes of the SPHN data, and fixers for declarations a query forgot or got wrong."""
from __future__ import annotations

import re

SPHN = "https://biomedit.ch/rdf/sphn-schema/sphn#"
INFERENCE_RULE_SET = "https://biomedit.ch/rdf/sphn-resource/global"

STANDARD_PREFIXES = {
    "sphn": SPHN,
    "sphn-individual": "https://biomedit.ch/rdf/sphn-schema/sphn/individual#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "icd-10-gm": "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/",
    "chop": "https://biomedit.ch/rdf/sphn-resource/chop/",
    "snomed": "http://snomed.info/id/",
    "loinc": "https://loinc.org/rdf/",
    "ucum": "https://biomedit.ch/rdf/sphn-resource/ucum/",
    "atc": "https://www.whocc.no/atc_ddd_index/?code=",
    "geno": "http://purl.obolibrary.org/obo/GENO_",
    "ordo": "http://www.orpha.net/ORDO/Orphanet_",
    "bif": "bif:",
}

PREFIX_DECLARATION_RE = re.compile(r"^\s*PREFIX\s+([A-Za-z][\w.-]*)?\s*:\s*<", re.IGNORECASE | re.MULTILINE)
PREFIX_NAMESPACE_RE = re.compile(r"((?<![\w?$:])PREFIX\s+([A-Za-z][\w.-]*)\s*:\s*)<([^<>\s]*)>", re.IGNORECASE)
LEADING_DEFINES_RE = re.compile(r"(?:\s*DEFINE[^\n]*\n)*", re.IGNORECASE)
IRIS_AND_STRINGS_RE = re.compile(r"<[^<>\s]*>|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
IRIS_STRINGS_COMMENTS_RE = re.compile(r"<[^<>\s]*>|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|#[^\n]*")
STRINGS_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
PREFIX_USE_RE = re.compile(r"(?<![\w?$:-])([A-Za-z][\w-]*):")
# prefix:SEG_rest, a prefixed term whose local name starts with a segment: doubled when the namespace ends in it.
DOUBLED_SEGMENT_RE = re.compile(r"(?<![\w?$:<.-])([A-Za-z][\w.-]*):([A-Za-z]+_)(\w+)")


def fix_standard_prefixes(text: str) -> str:
    """Point a standard prefix name declared with another namespace back at the data's namespace, and
    drop a namespace segment that a term repeats.

    The model declares `icd-10-gm:` as the OBO namespace, `geno:` under biomedit or `snomed:` as
    `http://snomed.info/sid/`, and every term under it then names nothing in the data. `bif:` declared
    as the W3C or the `/sparql/bif#` namespace is rejected by Virtuoso (SP030) and hides `bif:datediff`
    from the judge, so any `PREFIX bif:` becomes `<bif:>`. With `PREFIX obo: <.../obo/SO_>` the term
    `obo:SO_0001742` names `.../SO_SO_0001742`, which cannot exist: the repeated segment goes (q-046).
    A standard namespace declared under another prefix name is fine. The text comes back unchanged
    when every declaration and term is right.
    """
    without_strings = STRINGS_RE.sub(lambda match: " " * len(match.group(0)), text)
    for match in reversed(list(PREFIX_NAMESPACE_RE.finditer(without_strings))):
        head, name, namespace = match.groups()
        standard = STANDARD_PREFIXES.get(name)
        if standard is None or namespace == standard:
            continue
        text = text[:match.start()] + f"{head}<{standard}>" + text[match.end():]
    return _drop_doubled_segments(text)


def _drop_doubled_segments(text: str) -> str:
    """obo:SO_0001742 under a namespace ending in SO_ -> obo:0001742; terms inside strings, IRIs and
    comments are left alone. An undeclared standard prefix counts with its standard namespace, which
    add_missing_prefixes declares (geno:GENO_0000135 under .../obo/GENO_)."""
    without_strings = STRINGS_RE.sub(lambda match: " " * len(match.group(0)), text)
    declared = {name: namespace for _, name, namespace in PREFIX_NAMESPACE_RE.findall(without_strings)}
    masked = IRIS_STRINGS_COMMENTS_RE.sub(lambda match: " " * len(match.group(0)), text)
    for match in reversed(list(DOUBLED_SEGMENT_RE.finditer(masked))):
        name, segment, _rest = match.groups()
        namespace = declared.get(name, STANDARD_PREFIXES.get(name))
        if namespace and namespace.endswith(segment):
            text = text[:match.start(2)] + text[match.end(2):]
    return text


def add_missing_prefixes(text: str) -> str:
    """Declare standard prefixes that the query uses without declaring them."""
    declared = {match.group(1) or "" for match in PREFIX_DECLARATION_RE.finditer(text)}
    body = IRIS_AND_STRINGS_RE.sub(" ", text)
    used = set(PREFIX_USE_RE.findall(body))
    missing = [name for name in STANDARD_PREFIXES if name in used and name not in declared]
    if not missing:
        return text
    header = "".join(f"PREFIX {name}: <{STANDARD_PREFIXES[name]}>\n" for name in missing)
    defines_end = LEADING_DEFINES_RE.match(text).end()
    return text[:defines_end] + header + text[defines_end:]
