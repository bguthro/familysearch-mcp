import pytest

from familysearch.models import (
    FACT_BIRTH,
    build_fact,
    build_name,
    build_person,
)


def test_build_name_has_parts_and_fulltext():
    name = build_name("August", "Anderson")
    form = name["nameForms"][0]
    assert form["fullText"] == "August Anderson"
    parts = form["parts"]
    assert {"type": "http://gedcomx.org/Given", "value": "August"} in parts
    assert {"type": "http://gedcomx.org/Surname", "value": "Anderson"} in parts
    assert name["preferred"] is True


def test_build_fact_birth_with_date_place():
    fact = build_fact(FACT_BIRTH, date="1 January 1850", place="Nowhere")
    assert fact["type"] == FACT_BIRTH
    assert fact["date"]["original"] == "1 January 1850"
    assert fact["place"]["original"] == "Nowhere"


def test_build_fact_omits_absent_fields():
    fact = build_fact(FACT_BIRTH)
    assert "date" not in fact
    assert "place" not in fact


def test_build_person_maps_gender_and_living():
    person = build_person(given="A", surname="B", gender="Male", living=True)
    assert person["gender"]["type"] == "http://gedcomx.org/Male"
    assert person["living"] is True
    assert "persons" not in person
    assert [n["preferred"] for n in person["names"]].count(True) == 1
    assert len(person["names"]) == 1


def test_build_person_includes_facts():
    fact = build_fact(FACT_BIRTH, date="1850")
    person = build_person(given="A", surname="B", gender="Female", living=False, facts=[fact])
    assert person["facts"] == [fact]


def test_build_person_rejects_bad_gender():
    with pytest.raises(ValueError):
        build_person(given="A", surname="B", gender="X", living=True)
