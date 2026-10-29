"""Offline tests for the private-tree (Genealogies) fixtures of ``FixtureSet``."""
import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from tools.verify.fixtures import KINDS, FixtureSet

from _fake_genealogies import FakeGenealogies


def fixtures():
    return FixtureSet(FSClient("tkn", Environment.BETA, min_write_interval=0),
                      reason="ZZSPIKE fixture test")


def test_private_kinds_are_torn_down_last_and_trees_after_what_they_hold():
    assert KINDS[-5:] == ("genealogies_person", "genealogies_source", "genealogies_tree",
                           "group", "user_tree")


@responses.activate
def test_private_fixtures_touch_only_tracked_trees_and_never_the_shared_tree():
    fake = FakeGenealogies().serve()
    fx = fixtures()
    with pytest.raises(ValueError, match="tracked trees"):
        fx.genealogies_person("2:2:2:GZTR-777")
    with pytest.raises(ValueError, match="tracked trees"):
        fx.genealogies_source("2:2:2:GZTR-777", "ZZSPIKEx")
    with pytest.raises(ValueError, match="start with ZZSPIKE"):
        fx.genealogies_tree("My tree")
    tid = fx.genealogies_tree("ZZSPIKEtree")
    pid = fx.genealogies_person(tid, "Alive")
    assert fake.persons[pid]["living"] is True and fake.persons[pid]["tid"] == tid
    # every request went to /genealogies: a living person never reaches /tree/persons
    assert {c.request.url.split("/platform/")[1].split("/")[0] for c in responses.calls} == {
        "genealogies"}
    assert fx.tracked("genealogies_tree") == [tid] and fx.tracked("genealogies_person") == [pid]
    assert fx.teardown() == [] and fake.live() == []


@responses.activate
def test_teardown_deletes_persons_and_sources_before_the_tree():
    fake = FakeGenealogies().serve()
    fx = fixtures()
    tid = fx.genealogies_tree("ZZSPIKEtree")
    fx.genealogies_person(tid, "A")
    fx.genealogies_source(tid, "ZZSPIKEsrc")
    assert fx.teardown() == []
    deletes = [p for m, p in fake.calls if m == "DELETE"]
    assert [p.split("/genealogies/")[1].split("/")[0] for p in deletes] == [
        "persons", "sources", "trees"]


def test_created_persons_parses_the_live_tree_update_answer():
    from tools.verify.fixtures import created_persons
    ark = "https://beta.familysearch.org/ark:/61903/"
    body = {"sourceDescriptions": [
        {"id": "m1", "identifiers": {"http://gedcomx.org/Persistent": [ark + "2:2:MSGD-Z4F"]}},
        {"id": "f1", "identifiers": {"http://gedcomx.org/Persistent": [ark + "2:2:MSGD-Z4G"]}},
        {"id": "sd_0"}]}
    assert created_persons(body, {"m1", "f1"}) == {"m1": "2:2:MSGD-Z4F", "f1": "2:2:MSGD-Z4G"}
    assert created_persons({}, {"m1"}) == {}
