import json
from pathlib import Path

import pytest

from nl2sparql.guard import ComplianceGuard, PropertyKinds

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PREFIXES = (
    "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
    "PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
)


def load_examples() -> list[dict]:
    lines = (DATA_DIR / "examples.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture(scope="session")
def kinds() -> PropertyKinds:
    return PropertyKinds.load(DATA_DIR / "property_kinds.json")


@pytest.fixture(scope="session")
def guard(kinds) -> ComplianceGuard:
    return ComplianceGuard(kinds)
