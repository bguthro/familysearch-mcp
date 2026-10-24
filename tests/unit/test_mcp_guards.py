import pytest

from familysearch_mcp.guards import (
    AGGREGATOR_PATTERNS,
    classify_life_status,
    provenance_blocked,
)


def test_provenance_blocks_fold3_filename():
    assert provenance_blocked("1940 Fold3 Muster Roll.jpg", None) == "fold3"


def test_provenance_blocks_from_citation():
    assert provenance_blocked("photo.jpg", "Ancestry.com") == "ancestry"


def test_provenance_clear_returns_none():
    assert provenance_blocked("1940 census.pdf", "NARA") is None


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Newspapers_com Clipping.jpg", "newspapers.com"),
        ("Find a Grave memorial.png", "findagrave"),
        ("FOLD3 roll.jpg", "fold3"),
        ("my-heritage record.pdf", "myheritage"),
    ],
)
def test_provenance_separator_variants(filename, expected):
    assert provenance_blocked(filename, None) == expected


def test_provenance_none_and_empty():
    assert provenance_blocked("", None) is None
    assert provenance_blocked("", "") is None


def test_provenance_non_string_inputs_do_not_raise():
    assert provenance_blocked(123, ["x"]) is None  # type: ignore[arg-type]
    assert provenance_blocked(None, "Ancestry.com") == "ancestry"  # type: ignore[arg-type]


def test_provenance_filename_wins_when_both_match():
    assert provenance_blocked("fold3.jpg", "Ancestry.com") == "fold3"


def test_provenance_known_false_positive_pinned():
    assert provenance_blocked("Anderson Ancestry Chart.pdf", None) == "ancestry"


def test_aggregator_patterns_keys():
    assert set(AGGREGATOR_PATTERNS) == {
        "fold3", "ancestry", "newspapers.com", "myheritage", "findagrave"
    }


def cls(birth, death, **kw):
    kw.setdefault("today_year", 2026)
    return classify_life_status(birth, death, **kw)


def test_classify_deceased_with_death():
    assert cls("1990", "2020") == "deceased"


def test_classify_old_birth_no_death_is_deceased_by_age():
    assert cls("1850", None) == "deceased_by_age"


def test_classify_recent_birth_no_death_possibly_living():
    assert cls("1990", None) == "possibly_living"


@pytest.mark.parametrize("birth", [None, "", "unknown"])
def test_classify_unknown_birth_possibly_living(birth):
    assert cls(birth, None) == "possibly_living"


def test_classify_asserted_living():
    assert cls("1990", None, asserted_living=True) == "living"


def test_classify_asserted_living_with_death_is_conflict():
    assert cls("1990", "2020", asserted_living=True) == "possibly_living"


@pytest.mark.parametrize(
    "birth", ["1 January 1850", "25 December 1846", "abt 1850", "+1850-01-01"]
)
def test_classify_real_date_formats(birth):
    assert cls(birth, None) == "deceased_by_age"


def test_classify_boundary():
    assert cls("1916", None) == "possibly_living"
    assert cls("1915", None) == "deceased_by_age"


def test_classify_future_birth():
    assert cls("2030", None) == "possibly_living"


def test_classify_whitespace_death_is_absent():
    assert cls("1850", "   ") == "deceased_by_age"


@pytest.mark.parametrize(
    "birth",
    ["0830 3 June 1979", "1500 hrs 3 June 1979", "Lot 1234", "0830", "9999",
     "19790603", "3 Juni 1879"],
)
def test_classify_birth_fail_closed(birth):
    assert cls(birth, None) == "possibly_living"


@pytest.mark.parametrize(
    "birth",
    ["Between 1840 and 1850", "1840-1850", "Jan. 5, 1850", "5th of May 1850",
     "circa 1850", "c. 1850", "bef 1850", "1850 or 1851"],
)
def test_classify_birth_qualified_formats(birth):
    assert cls(birth, None) == "deceased_by_age"


@pytest.mark.parametrize("death", ["Unknown", "?", "N/A", "Living", "2999", 1990])
def test_classify_junk_death_is_ignored(death):
    assert cls("1990", death) == "possibly_living"
    assert cls("1990", death, asserted_living=True) == "living"
    assert cls("1850", death) == "deceased_by_age"


def test_classify_valid_deaths():
    assert cls("1950", "12 March 2019") == "deceased"
    assert cls("1990", "abt 1990") == "deceased"
    assert cls("1990", "abt 1990", asserted_living=True) == "possibly_living"


def test_classify_today_year_keyword_only():
    with pytest.raises(TypeError):
        classify_life_status("1990", None, 2026)  # type: ignore[misc]


@pytest.mark.parametrize(
    "filename",
    ["Find–a–Grave memorial.png", "Fold–3 roll.jpg",
     "Fold​3 roll.jpg", "Fold/3.jpg", "Fold３.jpg", "Fold 3.jpg"],
)
def test_provenance_unicode_and_separator_bypasses(filename):
    expected = "findagrave" if "Grave" in filename else "fold3"
    assert provenance_blocked(filename, None) == expected


@pytest.mark.parametrize(
    "prov,expected",
    [("ANCESTRY.COM/records", "ancestry"), ("newspapers com", "newspapers.com"),
     ("FindAGrave", "findagrave"), ("my heritage", "myheritage")],
)
def test_provenance_variants_in_provenance(prov, expected):
    assert provenance_blocked("clean.jpg", prov) == expected


def test_provenance_clean_with_nara():
    assert provenance_blocked("scan.jpg", "NARA") is None


@pytest.mark.parametrize(
    "birth",
    ["after 1850", "aft 1850", "from 1850", "From 1850 to 1860", "1850 99999"],
)
def test_classify_birth_lower_bound_and_long_digits_fail_closed(birth):
    assert cls(birth, None) == "possibly_living"


@pytest.mark.parametrize(
    "birth",
    ["bef 1850", "before 1850", "Between 1840 and 1850", "1 January 1850",
     "abt 1850", "3 June 1850", "5th of May 1850"],
)
def test_classify_birth_upper_bound_still_deceased_by_age(birth):
    assert cls(birth, None) == "deceased_by_age"


@pytest.mark.parametrize("death", ["after 1850", "from 1850"])
def test_classify_death_lower_bound_ok(death):
    assert cls("1800", death) == "deceased"
    assert cls("1800", death, asserted_living=True) == "possibly_living"


@pytest.mark.parametrize(
    "filename,expected",
    [("Fóld3.jpg", "fold3"), ("Ancéstry notes.pdf", "ancestry"),
     ("Myherıtage.png", "myheritage"), ("Fóld3.jpg", "fold3"),
     ("Ancéstry", "ancestry"), ("ﬁndagrave", "findagrave")],
)
def test_provenance_diacritics_and_ligatures(filename, expected):
    assert provenance_blocked(filename, None) == expected


def test_classify_non_string_inputs_do_not_raise():
    assert cls(1850, None) == "possibly_living"  # type: ignore[arg-type]
    assert cls(["1850"], None) == "possibly_living"  # type: ignore[arg-type]
    assert cls("1850", 2020) == "deceased_by_age"  # type: ignore[arg-type]
    assert cls("1850", ["x"]) == "deceased_by_age"  # type: ignore[arg-type]
