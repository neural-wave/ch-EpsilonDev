import json

import pytest

from scripts.compare_data_profile import compare


def write_profile(directory, property_kinds, classes, schema_terms):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "property_kinds.json").write_text(
        json.dumps(property_kinds), encoding="utf-8"
    )
    (directory / "schema_cards.json").write_text(
        json.dumps({"classes": classes, "schema_terms": schema_terms}), encoding="utf-8"
    )


def test_profile_comparison_reports_equal_profiles_as_aggregate_counts(tmp_path, capsys):
    kinds = {"urn:synthetic:property": {"kind": "literal", "count": 4}}
    classes = {
        "SyntheticClass": {
            "instances": 4,
            "patients": 2,
            "properties": {"syntheticValue": {"kind": "literal", "count": 4}},
        }
    }
    terms = ["SyntheticClass", "syntheticValue"]
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    write_profile(baseline, kinds, classes, terms)
    write_profile(candidate, kinds, classes, terms)

    assert compare(baseline, candidate) is True
    output = capsys.readouterr().out
    assert "profiles_match: true" in output
    assert "properties: baseline=1 profile=1 added=0 removed=0 changed=0" in output
    assert "SyntheticClass" not in output
    assert "syntheticValue" not in output


def test_profile_comparison_reports_only_aggregate_deltas(tmp_path, capsys):
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    write_profile(
        baseline,
        {
            "urn:synthetic:kept": {"kind": "literal", "count": 2},
            "urn:synthetic:removed": {"kind": "resource", "count": 1},
        },
        {
            "SyntheticKeep": {
                "instances": 2,
                "patients": 1,
                "properties": {"syntheticValue": {"kind": "literal", "count": 2}},
            },
            "SyntheticRemove": {
                "instances": 1,
                "patients": 1,
                "properties": {"syntheticGone": {"kind": "resource", "count": 1}},
            },
        },
        ["SyntheticKeep", "SyntheticRemove"],
    )
    write_profile(
        candidate,
        {
            "urn:synthetic:kept": {"kind": "literal", "count": 3},
            "urn:synthetic:added": {"kind": "individual", "count": 1},
        },
        {
            "SyntheticKeep": {
                "instances": 3,
                "patients": 1,
                "properties": {
                    "syntheticValue": {"kind": "literal", "count": 3},
                    "syntheticNew": {"kind": "individual", "count": 1},
                },
            },
            "SyntheticAdded": {"instances": 1, "patients": 1, "properties": {}},
        },
        ["SyntheticKeep", "SyntheticAdded"],
    )

    assert compare(baseline, candidate) is False
    output = capsys.readouterr().out
    assert "properties: baseline=2 profile=2 added=1 removed=1 changed=1" in output
    assert "classes: baseline=2 profile=2 added=1 removed=1 changed=1" in output
    assert "class_property_pairs: baseline=2 profile=2 added=1 removed=1 changed=1" in output
    assert "schema_terms: baseline=2 profile=2 added=1 removed=1 changed=0" in output
    assert "SyntheticKeep" not in output
    assert "syntheticNew" not in output
    assert "urn:synthetic" not in output


def test_profile_comparison_rejects_empty_profile(tmp_path):
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    write_profile(
        baseline,
        {"urn:synthetic:property": {"kind": "literal", "count": 1}},
        {"SyntheticClass": {"instances": 1, "patients": 1, "properties": {}}},
        ["SyntheticClass"],
    )
    write_profile(candidate, {}, {}, [])

    with pytest.raises(ValueError, match="empty or incomplete"):
        compare(baseline, candidate)
