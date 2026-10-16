import pytest

from nl2sparql.input_check import InputError, normalize_text


def test_keeps_clinical_characters():
    text = "Patients ≥ 65 with Sjögren and CRP > 5 µg/L at 38.5 °C?"
    assert normalize_text(text) == text


def test_collapses_whitespace_and_control_characters():
    assert normalize_text("  How many\tpatients\n\u0007have cholera?  ") == "How many patients have cholera?"


def test_removes_emoji():
    assert normalize_text("How many patients have cholera? 🤔🏥") == "How many patients have cholera?"


@pytest.mark.parametrize("text", ["", "   ", "\n\t", "🤔"])
def test_rejects_empty(text):
    with pytest.raises(InputError, match="must not be empty"):
        normalize_text(text)


def test_rejects_too_long():
    with pytest.raises(InputError, match="at most 10 characters"):
        normalize_text("x" * 11, max_chars=10)


def test_rejects_non_string():
    with pytest.raises(InputError, match="must be a string"):
        normalize_text(None)
