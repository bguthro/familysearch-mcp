"""Pure builders for GEDCOM X request-body fragments (no network)."""

GEDCOMX = "http://gedcomx.org/"

GENDER_URI = {
    "Male": GEDCOMX + "Male",
    "Female": GEDCOMX + "Female",
    "Unknown": GEDCOMX + "Unknown",
}

FACT_BIRTH = GEDCOMX + "Birth"
FACT_DEATH = GEDCOMX + "Death"
FACT_MARRIAGE = GEDCOMX + "Marriage"

_NAME_GIVEN = GEDCOMX + "Given"
_NAME_SURNAME = GEDCOMX + "Surname"


def build_name(
    given: str,
    surname: str,
    *,
    name_type: str = GEDCOMX + "BirthName",
    preferred: bool = True,
) -> dict:
    """Build a GEDCOM X name. `parts` are always emitted: FamilySearch
    requires them, `fullText` alone is not accepted."""
    return {
        "type": name_type,
        "preferred": preferred,
        "nameForms": [
            {
                "fullText": f"{given} {surname}".strip(),
                "parts": [
                    {"type": _NAME_GIVEN, "value": given},
                    {"type": _NAME_SURNAME, "value": surname},
                ],
            }
        ],
    }


def build_fact(fact_type: str, *, date: str | None = None, place: str | None = None) -> dict:
    fact: dict = {"type": fact_type}
    if date is not None:
        fact["date"] = {"original": date}
    if place is not None:
        fact["place"] = {"original": place}
    return fact


def build_person(
    *,
    given: str,
    surname: str,
    gender: str,
    living: bool,
    facts: list[dict] | None = None,
) -> dict:
    """Build a single GEDCOM X person (no `{"persons": [...]}` envelope)."""
    if gender not in GENDER_URI:
        raise ValueError(f"gender must be one of {sorted(GENDER_URI)}, got {gender!r}")
    person: dict = {
        "living": living,
        "gender": {"type": GENDER_URI[gender]},
        "names": [build_name(given, surname)],
    }
    if facts:
        person["facts"] = list(facts)
    return person
