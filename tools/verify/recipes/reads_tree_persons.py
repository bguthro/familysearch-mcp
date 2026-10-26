"""Read recipes for the person-centred Family Tree reads: persons, relatives, search, history.

Every case reads the fixture family (``dad`` + ``mom`` -> ``child``, built by
tools/verify/world.py) and anchors on a value the request does not send: a relative's
full name or id, the relationship ids, or a name that only the record itself carries.
Shapes and statuses follow the recon (docs/superpowers/recon/2026-10-25-api-surface)
and the live runs: relative lists answer 204 when empty (the childless/parentless
variants below, each after its populated case), ``readPersons`` needs ``pids``, change
histories are atom feeds. ``readPerson`` is verified by the pilot; ``headPerson`` only
uses it as its sibling GET. Tree search never surfaces fresh fixtures, so it reads a
stable real canary record (anchor-free). The reads that no obtainable fixture can
exercise under the read rules are in ``DEFERRED``.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from ..reads import ReadCase, json_path, reads_recipe

# Fits the tree-id format but is very unlikely to exist (as the pilot's).
NO_SUCH_PERSON = "ZZZZ-ZZZ"
# readPersons answers any unknown pid (well-formed or not) with an empty 204 on beta.
UNKNOWN_PIDS_EMPTY = (
    "beta answers readPersons for an unknown pid (ZZZZ-ZZZ, and NOT-A-PERSON-ID) with an "
    "empty 204 (live runs 1 and 2); the spec's 200/400/404/410 omits it, the known "
    "undocumented 204 for empty lists (recon README)")
# Live run 2: the relationship between just-created fixtures first answers 204.
RELATIONSHIP_EVENTUAL = (8, 5.0)
UNKNOWN_OPID_EMPTY = (
    "beta answers findRelationship for an unknown opid (ZZZZ-ZZZ) with an empty 204 (live "
    "run 3: no relationship and no such person look the same); beta also answers 204 "
    "while a fresh relationship is not computed yet (live run 2), so this 204 control "
    "counts only because the positive call in the same recipe returned content: both "
    "endpoints' names under their ids")

# ---------------------------------------------------------------- the search canary
# A real, deceased person in beta's tree (the coordinator's read-only probe): tree
# search does not index fresh fixtures (live runs 1-3), so searchTreePersons is checked
# against this stable record instead. It is real beta data: if it ever changes, the
# search case fails loudly, which is intended. Never hardcode these values elsewhere.
CANARY_ID = "GQYM-SVR"
CANARY_NAME = "Rosina Zopfi"
CANARY_BIRTH_DATE = "11 March 1870"
CANARY_COUNT = 5
CANARY_QUERY = {"flag": "fst", "q.givenName": "Rosina", "q.surname": "Zopfi",
                "q.birthLikeDate": "1870", "count": CANARY_COUNT}

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
        "an anchor could be discovered (readCurrentUser's personId and that person's "
        "name), but the not-found control cannot be built: the operation takes no path "
        "or query parameter, so every request to it reads the same signed-in user's "
        "person (the framework rejects a control identical to the positive, and an "
        "ignored extra parameter still answers the person, not 400/404/410/204)"),
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


# ------------------------------------------------------------------ cases


def _pid(role: str):
    return lambda w: {"pid": w.ids[role]}


def _name(role: str):
    return lambda w: w.names[f"{role}_name"]


def _id(role: str):
    return lambda w: w.ids[role]


MISSING_PID = {"pid": NO_SUCH_PERSON}
FAMILY = ("persons", "couple")

PERSONS_CASES = (
    # pids echoes both ids: the names are what the server must supply.
    ReadCase(op="readPersons", path_args=lambda w: {},
             query=lambda w: {"pids": f"{w.ids['child']},{w.ids['dad']}"},
             missing={}, missing_query={"pids": NO_SUCH_PERSON},
             control_may_be_empty=UNKNOWN_PIDS_EMPTY,
             expect=_expect_persons("child", "dad", exactly=True),
             anchors=(_name("child"), _name("dad")), needs=("persons",)),
    ReadCase(op="headPerson", path_args=_pid("child"), compare_with="readPerson",
             missing=MISSING_PID, expect=_expect_persons("child"),
             anchors=(_name("child"),), needs=("persons",)),
)
PERSONS = reads_recipe("reads.tree.persons", PERSONS_CASES)

RELATIVES_CASES = (
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
    # The dad's families: his spouse (couple) and their child (child-and-parents). Unlike
    # the child's /parents payload, it must hold the child and both relationships.
    ReadCase(op="readPersonFamilies", path_args=_pid("dad"), missing=MISSING_PID,
             expect=_both(_expect_persons("dad", "mom", "child"),
                          _expect_relationship("relationships", "couple"),
                          _expect_relationship("childAndParentsRelationships",
                                               "child_and_parents")),
             anchors=(_name("mom"), _name("child"), _id("couple"), _id("child_and_parents")),
             needs=FAMILY),
    # The relationship between the child and the dad: both persons, each under its own
    # name (the request sends only their ids). The live answer did not carry the
    # child-and-parents id, so it is not required.
    ReadCase(op="findRelationship",
             path_args=lambda w: {"pid": w.ids["child"], "opid": w.ids["dad"]},
             missing={"opid": NO_SUCH_PERSON},
             expect=_expect_persons("child", "dad"),
             anchors=(_name("child"), _name("dad")), needs=FAMILY,
             eventual=RELATIONSHIP_EVENTUAL, control_may_be_empty=UNKNOWN_OPID_EMPTY),
)
RELATIVES = reads_recipe("reads.tree.relatives", RELATIVES_CASES)

def _expect_canary_hits(result, world):
    """The canary search: exactly ``count`` hits, each a person with an id and a name and
    a confidence, the canary first (by id, name and birth date)."""
    entries = json_path(result.body, "entries")
    assert isinstance(entries, list) and len(entries) == CANARY_COUNT, (
        f"{len(entries) if isinstance(entries, list) else 'no'} entries, "
        f"not count={CANARY_COUNT}")
    for n, entry in enumerate(entries):
        person = json_path(entry, "content.gedcomx.persons[0]")
        assert isinstance(person.get("id"), str) and person["id"], f"hit {n} has no id"
        name = json_path(person, "display.name")
        assert isinstance(name, str) and name, f"hit {n} has no display name"
        confidence = entry.get("confidence")
        assert isinstance(confidence, (int, float)) and not isinstance(confidence, bool), (
            f"hit {n} has no confidence")
    first = json_path(entries[0], "content.gedcomx.persons[0]")
    assert first["id"] == CANARY_ID, f"first hit is {first['id']!r}, not {CANARY_ID}"
    assert json_path(first, "display.name") == CANARY_NAME, "first hit's name"
    assert json_path(first, "display.birthDate") == CANARY_BIRTH_DATE, "first hit's birth"


SEARCH_CASES = (
    ReadCase(op="searchTreePersons", path_args=lambda w: {}, query=dict(CANARY_QUERY),
             missing={}, missing_query={}, missing_statuses=(400,),
             expect=_expect_canary_hits,
             anchor_free_reason=("beta fresh fixtures are not indexed by tree search; "
                                 "verified against a stable real canary record"),
             literal_anchors=(CANARY_ID, CANARY_NAME, CANARY_BIRTH_DATE)),
)
SEARCH = reads_recipe("reads.tree.search", SEARCH_CASES)

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
