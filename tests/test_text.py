from thermalis.text import match_key, slugify, tr_lower, tr_upper


def test_turkish_case() -> None:
    assert tr_upper("istanbul") == "İSTANBUL"
    assert tr_lower("ISPARTA") == "ısparta"
    assert tr_lower("İSTANBUL") == "istanbul"


def test_match_key_folds_turkish() -> None:
    assert match_key("İSTANBUL") == match_key("istanbul") == match_key("Istanbul")
    assert match_key("  Kadıköy   Fikirtepe ") == "kadikoy fikirtepe"


def test_slugify() -> None:
    assert slugify("İstanbul Pilot") == "istanbul-pilot"
    assert slugify("Şişli / Gaziantep 2026") == "sisli-gaziantep-2026"
    assert slugify("!!!", fallback="project") == "project"
