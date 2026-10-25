"""Read recipes for the person-centred Family Tree reads: persons, relatives, search, history.

Every case reads the fixture family (``dad`` + ``mom`` -> ``child``, built by
tools/verify/world.py) and anchors on a value the request does not send: a relative's
full name or id, the relationship ids, or a name that only the record itself carries.
Shapes and statuses follow the recon (docs/superpowers/recon/2026-10-25-api-surface):
relative lists answer 204 when empty (the childless/parentless variants below, each
after its populated case), ``readPersons`` needs ``pids``, search needs ``flag=fst`` and
answers 400 without it, change histories are atom feeds. ``readPerson`` is verified by
the pilot; ``headPerson`` only uses it as its sibling GET. The reads that no obtainable
fixture can exercise under the read rules are in ``DEFERRED`` with the reason.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from ..reads import ReadCase, json_path, reads_recipe

# Fits the tree-id format but is very unlikely to exist (as the pilot's).
NO_SUCH_PERSON = "ZZZZ-ZZZ"
# Not a tree id at all. readPersons answers a well-formed unknown pid with 204 (live
# run), which its spec does not document, so an empty control cannot vouch for it; a
# pid that cannot be an id must be refused (400) by a server that parses the list.
MALFORMED_PERSON_ID = "NOT-A-PERSON-ID"
# A surname no fixture carries; sent without flag=fst, which beta answers 400.
NO_SUCH_SURNAME = "ZZSPIKENOSUCHSURNAME"
SEARCH_COUNT = 20
# The search index lags behind persons created a moment ago: up to 6 tries, 5 s apart.
SEARCH_EVENTUAL = (6, 5.0)

DEFERRED: dict[str, str] = {
    "readTreePersonMatches": (
        "beta answers 204 for every fixture (recon: dad and child); a 204 counts only "
        "after a populated case, which needs a possible-duplicate person the matcher has "
        "indexed, and the fixture world has no duplicate group"),
    "readTreePersonMatchesHeaders": (
        "its sibling GET readTreePersonMatches answers 204 for every fixture, and a HEAD "
        "case needs that GET to answer content to check anchors and validators"),
    "readPersonNotAMatches": (
        "beta answers 204 for every fixture; a populated case needs a not-a-match "
        "declaration, which is an unverified write the read tier cannot make"),
    "readPersonPortrait": (
        "documents only 204/301/307, so the only accepted answer is a redirect to a "
        "portrait image; recon got 204 even for the child with a memory, and setting a "
        "portrait is an unverified write"),
    "readPersonPortraits": (
        "beta answers 204 for every fixture (recon: dad and child, the child with a "
        "memory); a populated case needs a portrait, which is an unverified write"),
    "readPersonMerge": (
        "needs a viable merge pair; the fixture world's three persons are a couple and "
        "their son (parent-child and opposite-gender pairs), and recon's call answered 400"),
    "allowPersonMerge": (
        "its contrast needs one viable and one non-viable merge pair; the fixture world "
        "has no viable pair (no duplicate group), so a sound contrast cannot be built"),
    "performPersonMatchesByExample": (
        "ReadCase.body is static, not resolved against the world, so the example cannot "
        "describe a fixture created at run time; an anchor-free case would need a literal "
        "proven in every match response, and the recon never called this operation"),
    "readCurrentTreePerson": (
        "parameterless and scoped to the signed-in user's own tree person, which is no "
        "fixture (no anchor), and no query or body makes it answer not-found, so the "
        "not-found control cannot be built"),
}


# ------------------------------------------------------------------ checks


def _names_of(person: dict) -> set[str]:
    """Every name a GEDCOM X person carries: display.name and each nameForm's fullText."""
    names = set()
    display = person.get("display")
    if isinstance(display, dict) and isinstance(display.get("name"), str):
        names.add(display["name"])
    for name in person.get("names") or []:
        for form in (name.get("nameForms") or []) if isinstance(name, dict) else []:
            if isinstance(form, dict) and isinstance(form.get("fullText"), str):
                names.add(form["fullText"])
    return names


def _persons_by_id(persons) -> dict[str, dict]:
    if not isinstance(persons, list):
        raise AssertionError("persons is not a list")
    return {p["id"]: p for p in persons if isinstance(p, dict) and isinstance(p.get("id"), str)}


def _assert_person(persons: dict[str, dict], world, role: str) -> None:
    pid, name = world.ids[role], world.names[f"{role}_name"]
    if pid not in persons:
        raise AssertionError(f"the {role} {pid} is not among the persons")
    if name not in _names_of(persons[pid]):
        raise AssertionError(f"the {role} {pid} does not carry its name {name!r}")


def _expect_persons(*roles: str, exactly: bool = False):
    """``persons`` holds each role's person (by id) under its own name."""

    def expect(result, world):
        persons = _persons_by_id(json_path(result.body, "persons"))
        for role in roles:
            _assert_person(persons, world, role)
        if exactly:
            want = {world.ids[r] for r in roles}
            assert set(persons) == want, (
                f"persons are {sorted(persons)}, not the requested {sorted(want)}")

    return expect


def _expect_relationship(key: str, role: str):
    """Also: the ``key`` relationship list holds the fixture relationship ``role``."""

    def check(result, world):
        rels = json_path(result.body, key)
        rels = rels if isinstance(rels, list) else []
        ids = {r.get("id") for r in rels if isinstance(r, dict)}
        assert world.ids[role] in ids, f"{key} does not hold the {role} {world.ids[role]}"

    return check


def _both(*checks):
    def expect(result, world):
        for check in checks:
            check(result, world)

    return expect


def _no_relatives(what: str):
    """The empty variant: the person has no ``what``, so any content is wrong."""

    def expect(result, world):
        raise AssertionError(f"answered content for a person with no {what}")

    return expect


def _expect_history(role: str):
    """A change feed about ``role``: linked to the person, with an entry naming it."""

    def expect(result, world):
        pid, name = world.ids[role], world.names[f"{role}_name"]
        href = json_path(result.body, "links.person.href")
        # Beta links it with a query (``?flag=fsh``): compare the path only.
        path = urlsplit(href).path.rstrip("/") if isinstance(href, str) else ""
        assert path.endswith("/" + pid), (
            f"links.person.href {href!r} is not the {role} {pid}")
        entries = json_path(result.body, "entries")
        assert isinstance(entries, list) and entries, "no change entries"
        assert any(name in json.dumps(e, ensure_ascii=False) for e in entries), (
            f"no change entry carries the {role}'s name {name!r}")

    return expect


def _expect_search_hits(*roles: str):
    """Each role is a search hit: an entry whose gedcomx person is it, under its name."""

    def expect(result, world):
        entries = json_path(result.body, "entries")
        assert isinstance(entries, list), "entries is not a list"
        found: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            gx = (entry.get("content") or {}).get("gedcomx") or {}
            persons = [p for p in gx.get("persons") or [] if isinstance(p, dict)]
            found.update(_persons_by_id(persons))
            # A hit's person may carry its id only as the entry id (as
            # projections.project_search_hit reads it): the hit is still that person.
            if len(persons) == 1 and "id" not in persons[0] and isinstance(
                    entry.get("id"), str):
                found[entry["id"]] = persons[0]
        for role in roles:
            _assert_person(found, world, role)

    return expect


# ------------------------------------------------------------------ cases


def _pid(role: str):
    return lambda w: {"pid": w.ids[role]}


def _name(role: str):
    return lambda w: w.names[f"{role}_name"]


def _id(role: str):
    return lambda w: w.ids[role]


MISSING_PID = {"pid": NO_SUCH_PERSON}
FAMILY = ("persons", "couple")

PERSONS = reads_recipe("reads.tree.persons", [
    # pids echoes both ids: the names are what the server must supply.
    ReadCase(op="readPersons", path_args=lambda w: {},
             query=lambda w: {"pids": f"{w.ids['child']},{w.ids['dad']}"},
             missing={}, missing_query={"pids": MALFORMED_PERSON_ID}, missing_statuses=(400,),
             expect=_expect_persons("child", "dad", exactly=True),
             anchors=(_name("child"), _name("dad")), needs=("persons",)),
    ReadCase(op="headPerson", path_args=_pid("child"), compare_with="readPerson",
             missing=MISSING_PID, expect=_expect_persons("child"),
             anchors=(_name("child"),), needs=("persons",)),
])

RELATIVES = reads_recipe("reads.tree.relatives", [
    ReadCase(op="readPersonParents", path_args=_pid("child"), missing=MISSING_PID,
             expect=_both(_expect_persons("dad", "mom"),
                          _expect_relationship("childAndParentsRelationships",
                                               "child_and_parents")),
             anchors=(_name("dad"), _name("mom"), _id("child_and_parents")), needs=FAMILY),
    ReadCase(op="readPersonParents", path_args=_pid("dad"), missing=MISSING_PID,
             expect=_no_relatives("parents"), allow_empty=True, needs=FAMILY),
    ReadCase(op="readPersonChildren", path_args=_pid("dad"), missing=MISSING_PID,
             expect=_expect_persons("child"),
             anchors=(_id("child"), _name("child")), needs=FAMILY),
    ReadCase(op="readPersonChildren", path_args=_pid("child"), missing=MISSING_PID,
             expect=_no_relatives("children"), allow_empty=True, needs=FAMILY),
    ReadCase(op="readPersonSpouses", path_args=_pid("dad"), missing=MISSING_PID,
             expect=_both(_expect_persons("mom"),
                          _expect_relationship("relationships", "couple")),
             anchors=(_id("mom"), _name("mom"), _id("couple")), needs=FAMILY),
    ReadCase(op="readPersonSpouses", path_args=_pid("child"), missing=MISSING_PID,
             expect=_no_relatives("spouses"), allow_empty=True, needs=FAMILY),
    ReadCase(op="readPersonFamilies", path_args=_pid("child"), missing=MISSING_PID,
             expect=_both(_expect_persons("dad", "mom", "child"),
                          _expect_relationship("childAndParentsRelationships",
                                               "child_and_parents")),
             anchors=(_name("dad"), _name("mom"), _id("child_and_parents")), needs=FAMILY),
    # The relationship between the child and the dad: both persons, each under its own
    # name (the request sends only their ids). The live answer did not carry the
    # child-and-parents id, so it is not required.
    ReadCase(op="findRelationship",
             path_args=lambda w: {"pid": w.ids["child"], "opid": w.ids["dad"]},
             missing={"opid": NO_SUCH_PERSON},
             expect=_expect_persons("child", "dad"),
             anchors=(_name("child"), _name("dad")), needs=FAMILY),
])

SEARCH = reads_recipe("reads.tree.search", [
    # The proven shape (Search.tree_search): flag=fst and a surname, here the run's own,
    # which every fixture shares; the hits must be the fixtures by id and full name.
    ReadCase(op="searchTreePersons", path_args=lambda w: {},
             query=lambda w: {"flag": "fst", "q.surname": w.names["child_name"].split()[-1],
                              "count": SEARCH_COUNT},
             missing={}, missing_query={"q.surname": NO_SUCH_SURNAME}, missing_statuses=(400,),
             expect=_expect_search_hits("child", "dad", "mom"),
             anchors=(_name("child"), _id("child"), _id("dad")), needs=("persons",),
             eventual=SEARCH_EVENTUAL),
])

HISTORY = reads_recipe("reads.tree.history", [
    # The pid is in the path and in every link: the name is the anchor it cannot echo.
    ReadCase(op="readPersonChangeHistory", path_args=_pid("child"), missing=MISSING_PID,
             expect=_expect_history("child"), anchors=(_name("child"),),
             needs=("persons",)),
    ReadCase(op="headPersonChangeHistory", path_args=_pid("child"),
             compare_with="readPersonChangeHistory", missing=MISSING_PID,
             expect=_expect_history("child"), anchors=(_name("child"),),
             needs=("persons",)),
])

READS_TREE_PERSONS = (PERSONS, RELATIVES, SEARCH, HISTORY)
