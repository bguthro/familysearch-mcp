"""Relationship writes: create (couple and child-and-parents), update, delete, restore,
and removing a parent from a child-and-parents relationship.

The hard lesson (docs/api-discovery.md, Corrections): a parent-child POST once came
back 201 and made COUPLE links between the child and each parent. So the create
recipe reads the relationship TYPE back: the couple from its couple resource and the
husband's spouses, the child-and-parents relationship from its own resource and the
child's parents and the parent's children, and never as a spouse of the child.

Fixture hygiene: every relationship id is tracked under the kind its ``Location``
names the moment the create answers (so teardown deletes what was really made, not
what was asked for); relationships joining only this run's persons that nobody asked
for (beta derives a couple for a two-parent child-and-parents relationship whose
parents have none) are found, tracked and fail the recipe. A deleted-then-restored
relationship stays tracked: strict teardown deletes it again and checks it is gone.
Recipes other than the create build their relationships with the proven library
(``FixtureSet.couple`` / ``child_and_parents``), never with the op under test.
"""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from familysearch.api._runtime import ApiResult
from familysearch.models import FACT_MARRIAGE, GEDCOMX, build_fact

from ..core import Recipe, VerifyContext
from .writes_common import (COUPLE, GONE, Relationship, Relatives, describe, expect_same,
                            poll, read_child_and_parents, read_couple, relationship_view,
                            relatives, restore, status_of, token)

FACT_DIVORCE = GEDCOMX + "Divorce"
ADOPTIVE = GEDCOMX + "AdoptiveParent"
PATHS = {"couple": "/couple-relationships/",
         "child_and_parents": "/child-and-parents-relationships/"}
KIND_NAMES = {"couple": "a couple relationship",
              "child_and_parents": "a child-and-parents relationship"}

# Relationship writes that no recipe claims, with the evidence why (for p3-deferred.md).
DEFERRED: dict[str, str] = {
    "restoreChildAndParentsRelationship": (
        "live run 2026-10-26 (code d0ba628): after a proven delete (listed -> unlisted, "
        "then 410), the restore of the one-parent child-and-parents relationship "
        "answered 204 (Content-Location only) but the relationship stayed 410 and "
        "unlisted from the child's parents for 40 s (8 polls x 5 s), under the old id "
        "and any other; the same op shape restores couples (couple_delete_restore "
        "passed). Left for the change-history/restore tier (P6/P9) to look into via "
        "the relationship's /changes"),
}


# ------------------------------------------------------------------ tracking


def _sweep(ctx: VerifyContext, pids: Iterable[str]) -> list[str]:
    """Track every relationship of ``pids`` (this run's persons) not tracked yet.

    Read from each person's spouses and parents; only relationships whose persons
    are all this run's are taken. Returns the ids it tracked.
    """
    own = set(ctx.fixtures.tracked("person"))
    found = []
    for pid in pids:
        for which in ("spouses", "parents"):
            for rid, rel in relatives(ctx, which, pid).relationships.items():
                kind = rel.kind if rel.kind in PATHS else None
                if not kind or not rel.persons <= own or rid in ctx.fixtures.tracked(kind):
                    continue
                ctx.fixtures.track(kind, rid)
                found.append(rid)
    return found


def _created(ctx: VerifyContext, result: ApiResult, posted: str, pids: list[str]) -> str:
    """The new relationship's id, tracked under the kind its Location names.

    A create that names no id, or one of another kind than ``posted``, fails, after
    every untracked relationship of ``pids`` is swept up for teardown.
    """
    lowered = {k.lower(): v for k, v in result.headers.items()}
    location = urlsplit(lowered.get("location") or "").path
    rid = lowered.get("x-entity-id") or location.rstrip("/").rsplit("/", 1)[-1]
    kind = next((k for k, p in PATHS.items() if p in location), posted)
    if not rid:
        swept = _sweep(ctx, pids)
        raise AssertionError(f"createRelationship answered no X-ENTITY-ID or Location "
                             f"(swept up {swept}); {describe(result)}")
    ctx.fixtures.track(kind, rid)
    if kind != posted:
        swept = _sweep(ctx, pids)
        raise AssertionError(
            f"posting {KIND_NAMES[posted]} created {rid} at {location!r}, which is "
            f"{KIND_NAMES[kind]}, not {KIND_NAMES[posted]} (swept up {swept})")
    return rid


def _expect_kind(rel: Relationship, kind: str, roles: dict[str, str]) -> None:
    if rel.kind != kind or dict(rel.roles) != roles:
        raise AssertionError(f"relationship {rel.id} reads back as {rel.summary()}, not "
                             f"{KIND_NAMES[kind]} with {roles}; {describe(rel.result)}")


# ------------------------------------------------------------------ create


def relationship_create(ctx: VerifyContext) -> None:
    """createRelationship with both bodies: a couple, then ONE child-and-parents.

    The couple (husband + wife) is posted as a ``relationships`` Couple; the
    child-and-parents as ``childAndParentsRelationships`` with parent1 (husband),
    parent2 (wife) and child, after the couple, so beta derives no couple of its own.
    Each id is tracked under the kind its Location names; a kind other than the one
    posted fails (the original bug made couples for a parent-child post).

    Read-backs: the couple's resource is a Couple joining the two, and the husband's
    spouses list it (polled); the child-and-parents resource names the parents and the
    child in their roles, the child's parents and the husband's children list it
    (polled), and the child's spouses list nobody. Afterwards the husband's spouses
    hold exactly the one couple to the wife: any other is tracked and fails.
    Negative control: the couple's id in the husband's spouses, and the
    child-and-parents id in the child's parents, before (absent) and after.
    """
    husband = ctx.fixtures.person(given="Husband")
    wife = ctx.fixtures.person(given="Wife", gender="Female")
    child = ctx.fixtures.person(given="Child", birth_year="1880")
    family = [husband, wife, child]
    ref = ctx.fixtures.person_ref
    spouses0 = relatives(ctx, "spouses", husband)
    parents0 = relatives(ctx, "parents", child)

    created = ctx.api.create_relationship(reason=ctx.reason, body={"relationships": [{
        "type": COUPLE, "person1": ref(husband), "person2": ref(wife)}]})
    crid = _created(ctx, created, "couple", family)
    _expect_kind(read_couple(ctx, crid), "couple", {"person1": husband, "person2": wife})
    spouses1 = poll(ctx, lambda: relatives(ctx, "spouses", husband),
                    lambda s: crid in s.relationships, f"readPersonSpouses: couple {crid}")
    ctx.expect_changed(crid in spouses0.relationships, crid in spouses1.relationships,
                       f"presence of couple {crid} among the husband's spouses")
    if spouses1.relationships[crid].kind != "couple" or wife not in spouses1.persons:
        raise AssertionError(f"the husband's spouses do not list {crid} as a couple with "
                             f"the wife {wife}; {describe(spouses1.result)}")

    created = ctx.api.create_relationship(reason=ctx.reason, body={
        "childAndParentsRelationships": [{
            "parent1": ref(husband), "parent2": ref(wife), "child": ref(child)}]})
    caprid = _created(ctx, created, "child_and_parents", family)
    try:
        _check_child_and_parents(ctx, caprid, crid, husband, wife, child, parents0)
    except Exception:
        _sweep_quietly(ctx, family)  # a couple beta derived must not outlive a failure
        raise


def _sweep_quietly(ctx: VerifyContext, pids: list[str]) -> None:
    """``_sweep`` for teardown's sake only: what it tracks (or fails at) is a note."""
    try:
        swept = _sweep(ctx, pids)
    except Exception as e:  # noqa: BLE001 - the recipe's own outcome is the one to report
        ctx.notes.append(f"sweeping up untracked relationships failed: {e}")
        return
    if swept:
        ctx.notes.append(f"tracked relationships nobody asked for, for teardown: {swept}")


def _check_child_and_parents(ctx: VerifyContext, caprid: str, crid: str, husband: str,
                             wife: str, child: str, parents0) -> None:
    family = [husband, wife, child]
    _expect_kind(read_child_and_parents(ctx, caprid), "child_and_parents",
                 {"parent1": husband, "parent2": wife, "child": child})
    parents1 = poll(ctx, lambda: relatives(ctx, "parents", child),
                    lambda s: caprid in s.relationships,
                    f"readPersonParents: child-and-parents {caprid}")
    ctx.expect_changed(caprid in parents0.relationships, caprid in parents1.relationships,
                       f"presence of child-and-parents {caprid} among the child's parents")
    if parents1.relationships[caprid].kind != "child_and_parents":
        raise AssertionError(f"the child's parents list {caprid} as "
                             f"{parents1.relationships[caprid].summary()}")
    poll(ctx, lambda: relatives(ctx, "children", husband),
         lambda s: caprid in s.relationships and child in s.persons,
         f"readPersonChildren: the child through {caprid}")
    spouses = relatives(ctx, "spouses", child)
    if spouses.persons - {child} or spouses.of_kind("couple"):
        swept = _sweep(ctx, family)
        raise AssertionError(f"the child has spouse links after the parent-child create "
                             f"(swept up {swept}); {describe(spouses.result)}")
    couples = [rid for rid, r in relatives(ctx, "spouses", husband).of_kind(
        "couple").items() if r.persons == {husband, wife}]
    if couples != [crid]:
        swept = _sweep(ctx, family)
        raise AssertionError(f"the husband and wife have couples {couples}: those other "
                             f"than {crid} were not created by this recipe (tracked "
                             f"{swept} for teardown)")


# ------------------------------------------------------------------ update


def _same_facts(before: Relationship, after: Relationship, key: str, what: str) -> None:
    kept = {fid: after.facts.get(key, {}).get(fid) for fid in before.facts.get(key, {})}
    expect_same(dict(before.facts.get(key, {})), kept, what)


def _one_new(before: Relationship, after: Relationship, key: str) -> str:
    new = set(after.facts.get(key, {})) - set(before.facts.get(key, {}))
    if len(new) != 1:
        raise AssertionError(f"expected one new fact in {key}, found {sorted(new)}: "
                             f"{after.summary()}; {describe(after.result)}")
    return new.pop()


def couple_update(ctx: VerifyContext) -> None:
    """updateCoupleRelationship twice on a fixture couple: add a Marriage, then a Divorce.

    Each fact carries a fresh place marker and is read back from the couple's
    resource. Negative control: no fact carries the marker before its update, exactly
    one new fact of the posted type does after. Siblings: the couple's persons are
    unchanged, and the Divorce update keeps the Marriage (by id and value), so an
    update that replaces all facts fails.
    """
    husband = ctx.fixtures.person(given="UpdHusband")
    wife = ctx.fixtures.person(given="UpdWife", gender="Female")
    crid = ctx.fixtures.couple(husband, wife)
    before = read_couple(ctx, crid)
    _expect_kind(before, "couple", {"person1": husband, "person2": wife})
    for fact_type, tag in ((FACT_MARRIAGE, "w"), (FACT_DIVORCE, "v")):
        place = token(tag)
        ctx.api.update_couple_relationship(crid, reason=ctx.reason, body={"relationships": [
            {"facts": [build_fact(fact_type, date="1874", place=place)]}]})
        after = read_couple(ctx, crid)
        ctx.expect_changed(bool(before.facts_with(place)), bool(after.facts_with(place)),
                           f"presence of a couple fact marked {place}")
        fid = _one_new(before, after, "facts")
        fact = after.facts["facts"][fid]
        if fact.type != fact_type or fact.place != place:
            raise AssertionError(f"the new fact {fid} is not the {fact_type} at {place}: "
                                 f"{after.summary()}")
        _same_facts(before, after, "facts", "the earlier facts (on adding one)")
        expect_same(dict(before.roles), dict(after.roles), "the couple's persons")
        before = after


def child_and_parents_update(ctx: VerifyContext) -> None:
    """updateChildAndParentsRelationship twice: an adoptive lineage for parent1, then for
    parent2.

    The fixture relationship has two parents (their couple made first, so beta derives
    none). Each update posts one ``AdoptiveParent`` fact in ``parent1Facts`` /
    ``parent2Facts`` and is read back from the relationship's resource.
    Negative control: the ids of that list's adoptive facts before (none) and after.
    Siblings: the parents and the child are unchanged; the parent1 update leaves
    ``parent2Facts`` as they were and the parent2 update leaves ``parent1Facts``, so an
    update that replaces every fact fails.
    """
    husband = ctx.fixtures.person(given="CapUpdFather")
    wife = ctx.fixtures.person(given="CapUpdMother", gender="Female")
    child = ctx.fixtures.person(given="CapUpdChild", birth_year="1880")
    ctx.fixtures.couple(husband, wife)
    caprid = ctx.fixtures.child_and_parents(child, [husband, wife])
    try:
        _update_parent_facts(ctx, caprid, {"parent1": husband, "parent2": wife,
                                           "child": child})
    finally:  # should beta derive a couple anyway, teardown deletes it too
        _sweep_quietly(ctx, [husband, wife, child])


def _update_parent_facts(ctx: VerifyContext, caprid: str, roles: dict[str, str]) -> None:
    before = read_child_and_parents(ctx, caprid)
    _expect_kind(before, "child_and_parents", roles)
    for key, other in (("parent1Facts", "parent2Facts"), ("parent2Facts", "parent1Facts")):
        ctx.api.update_child_and_parents_relationship(caprid, reason=ctx.reason, body={
            "childAndParentsRelationships": [{key: [{"type": ADOPTIVE}]}]})
        after = read_child_and_parents(ctx, caprid)

        def adoptive(rel: Relationship, key: str = key) -> set[str]:
            return {fid for fid, f in rel.facts.get(key, {}).items() if f.type == ADOPTIVE}

        ctx.expect_changed(adoptive(before), adoptive(after), f"the adoptive facts in {key}")
        fid = _one_new(before, after, key)
        if after.facts[key][fid].type != ADOPTIVE:
            raise AssertionError(f"the new {key} fact {fid} is not {ADOPTIVE}: "
                                 f"{after.summary()}")
        _same_facts(before, after, other, f"{other} (on updating {key})")
        _expect_kind(after, "child_and_parents", roles)
        before = after


# ------------------------------------------------------------------ delete, restore


def _restored_id(result: ApiResult, kind: str) -> str | None:
    """The id a restore names for the relationship, if any (X-ENTITY-ID, or a Location
    at the relationship's own kind of resource)."""
    lowered = {k.lower(): v for k, v in result.headers.items()}
    if lowered.get("x-entity-id"):
        return lowered["x-entity-id"]
    location = urlsplit(lowered.get("location") or "").path
    if PATHS[kind] in location:
        return location.rstrip("/").rsplit("/", 1)[-1] or None
    return None


def _delete_restore(ctx: VerifyContext, rid: str, kind: str, read, delete, do_restore,
                    listed, persons: frozenset[str]) -> None:
    """Delete ``rid``, read it gone, restore it, read it back with the same persons.

    ``read(rid)`` is the relationship resource read; ``listed()`` answers the ids of
    the ``kind`` relationships joining exactly ``persons`` in a person's relatives.
    Both are polled (beta's relationship reads lag). The restore may bring the
    relationship back under a new id (named by its X-ENTITY-ID / Location, or seen
    only in the list): that id is tracked at once and checked instead, and the old
    one, read gone, is left out of teardown.
    """
    what = KIND_NAMES[kind].split(" ", 1)[1]
    s0 = status_of(lambda: read(rid))
    if s0 != 200:
        raise AssertionError(f"the fixture {what} {rid} read back {s0} before the delete")
    poll(ctx, listed, lambda ids: rid in ids, f"the {what} {rid} listed before the delete",
         result_of=lambda v: None)
    delete(rid)
    s1 = status_of(lambda: read(rid))
    ctx.expect_changed(s0, s1, f"the {what}'s readability after delete")
    if s1 not in GONE:
        raise AssertionError(f"deleted {what} {rid} read back {s1}, not 404/410")
    poll(ctx, listed, lambda ids: rid not in ids,
         f"the {what} {rid} unlisted after the delete", result_of=lambda v: None)
    restored = restore(lambda body: do_restore(rid, body))
    shown = {k: v for k, v in restored.headers.items()
             if k.lower() in ("location", "x-entity-id", "content-location")}
    ctx.notes.append(f"restore answered {restored.status} {shown}")
    named = _restored_id(restored, kind)
    if named and named != rid:
        ctx.fixtures.track(kind, named)

    def readable() -> dict[str, int]:
        """Each candidate id's status: the old one, the one the restore named, and
        any the list shows joining the same persons."""
        ids = {rid, *([named] if named else []), *listed()}
        return {i: status_of(lambda i=i: read(i)) for i in ids}

    statuses = poll(ctx, readable, lambda s: 200 in s.values(),
                    f"the {what} readable again after the restore",
                    result_of=lambda v: None)
    back_id = rid if statuses.get(rid) == 200 else sorted(
        i for i, s in statuses.items() if s == 200)[0]
    if back_id != rid:
        if back_id not in ctx.fixtures.tracked(kind):
            ctx.fixtures.track(kind, back_id)
        ctx.fixtures.mark_deleted(kind, rid)  # read gone above; never restored
        ctx.notes.append(f"the {what} {rid} was restored under a new id {back_id}")
    ctx.expect_changed(s1 == 200, statuses[back_id] == 200,
                       f"the {what}'s readability after restore")
    back = relationship_view(read(back_id), back_id)
    if back.kind != kind or back.persons != persons:
        raise AssertionError(f"restored {what} {back_id} reads back {back.summary()}, was "
                             f"{KIND_NAMES[kind]} of {sorted(persons)}; "
                             f"{describe(back.result)}")
    poll(ctx, listed, lambda ids: back_id in ids,
         f"the {what} {back_id} listed again after the restore", result_of=lambda v: None)


def _joining(rels: Relatives, kind: str, persons: frozenset[str]) -> set[str]:
    return {i for i, r in rels.of_kind(kind).items() if r.persons == persons}


def couple_delete_restore(ctx: VerifyContext) -> None:
    """Delete a fixture couple, read it gone, restore it, read it back.

    Negative controls, on the couple resource's status: readable -> 404/410 after the
    delete, gone -> 200 after the restore (polled; a no-op never reads 200 and fails).
    The husband's spouses list it before, not after the delete (polled), and again
    after the restore, which must join the same two persons. Should beta restore it
    under a new id, that id is tracked and checked instead. The restored couple stays
    tracked: teardown deletes it again and checks it is gone.
    """
    husband = ctx.fixtures.person(given="DelHusband")
    wife = ctx.fixtures.person(given="DelWife", gender="Female")
    crid = ctx.fixtures.couple(husband, wife)
    api, reason = ctx.api, ctx.reason
    persons = frozenset({husband, wife})
    _delete_restore(
        ctx, crid, "couple", api.read_couple_relationship,
        lambda rid: api.delete_couple_relationship(rid, reason=reason),
        lambda rid, body: api.restore_couple_relationship(rid, reason=reason, body=body),
        lambda: _joining(relatives(ctx, "spouses", husband), "couple", persons), persons)


def child_and_parents_delete(ctx: VerifyContext) -> None:
    """Delete one of two fixture child-and-parents relationships; read it gone.

    One parent with two children, a relationship each (one parent: beta derives no
    couple). Negative controls: the deleted relationship's resource status, readable
    before and 404/410 after; its id in the child's parents list, before and (polled)
    not after. Sibling: the other child's relationship still reads 200 with the same
    parent and child, and is still listed among the parent's children. The deleted one
    is marked deleted once read gone, so teardown does not look for it again.
    restoreChildAndParentsRelationship is not called: see ``DEFERRED``.
    """
    parent = ctx.fixtures.person(given="DelParent")
    child = ctx.fixtures.person(given="DelChild", birth_year="1880")
    sibling = ctx.fixtures.person(given="DelSibling", birth_year="1882")
    caprid = ctx.fixtures.child_and_parents(child, [parent])
    other = ctx.fixtures.child_and_parents(sibling, [parent])
    api = ctx.api
    persons = frozenset({parent, child})
    listed = lambda: _joining(relatives(ctx, "parents", child),  # noqa: E731
                              "child_and_parents", persons)
    s0 = status_of(lambda: api.get_child_and_parents_relationship(caprid))
    if s0 != 200:
        raise AssertionError(f"the fixture child-and-parents {caprid} read back {s0}")
    before = poll(ctx, listed, lambda ids: caprid in ids,
                  f"child-and-parents {caprid} among the child's parents",
                  result_of=lambda v: None)
    other0 = read_child_and_parents(ctx, other)
    api.delete_child_and_parents_relationship(caprid, reason=ctx.reason)
    s1 = status_of(lambda: api.get_child_and_parents_relationship(caprid))
    ctx.expect_changed(s0, s1, f"the readability of child-and-parents {caprid}")
    if s1 not in GONE:
        raise AssertionError(f"deleted child-and-parents {caprid} read back {s1}, "
                             "not 404/410")
    after = poll(ctx, listed, lambda ids: caprid not in ids,
                 f"child-and-parents {caprid} unlisted after the delete",
                 result_of=lambda v: None)
    ctx.expect_changed(caprid in before, caprid in after,
                       f"presence of child-and-parents {caprid} among the child's parents")
    ctx.fixtures.mark_deleted("child_and_parents", caprid)
    other1 = read_child_and_parents(ctx, other)
    expect_same(dict(other0.roles), dict(other1.roles),
                f"the other child's relationship {other}")
    poll(ctx, lambda: relatives(ctx, "children", parent),
         lambda s: other in s.relationships, f"child-and-parents {other} still listed")

def relationship_parent_delete(ctx: VerifyContext) -> None:
    """Remove parent2 from a fixture two-parent child-and-parents relationship.

    The parents' couple is made first (so beta derives none). The role sent is the key
    the relationship's resource answers the second parent under (``parent2``).
    Negative control: the person in that role before (the mother) and after (nobody).
    Siblings: parent1 and the child read back unchanged, the relationship is still
    readable, and so is the parents' couple.
    """
    father = ctx.fixtures.person(given="ParFather")
    mother = ctx.fixtures.person(given="ParMother", gender="Female")
    child = ctx.fixtures.person(given="ParChild", birth_year="1880")
    crid = ctx.fixtures.couple(father, mother)
    caprid = ctx.fixtures.child_and_parents(child, [father, mother])
    try:
        _delete_parent(ctx, caprid, crid, father, mother, child)
    finally:  # should beta derive a couple anyway, teardown deletes it too
        _sweep_quietly(ctx, [father, mother, child])


def _delete_parent(ctx: VerifyContext, caprid: str, crid: str, father: str, mother: str,
                   child: str) -> None:
    v0 = read_child_and_parents(ctx, caprid)
    _expect_kind(v0, "child_and_parents",
                 {"parent1": father, "parent2": mother, "child": child})
    role = "parent2"
    ctx.api.delete_child_and_parents_relationship_parent(caprid, role, reason=ctx.reason)
    v1 = read_child_and_parents(ctx, caprid)
    ctx.expect_changed(v0.roles.get(role), v1.roles.get(role),
                       f"the {role} of child-and-parents {caprid}")
    if role in v1.roles:
        raise AssertionError(f"{caprid} still has a {role} after deleting it: "
                             f"{v1.summary()}")
    expect_same({k: v for k, v in v0.roles.items() if k != role}, dict(v1.roles),
                "the other parent and the child")
    if status_of(lambda: ctx.api.read_couple_relationship(crid)) != 200:
        raise AssertionError(f"the parents' couple {crid} is no longer readable")


WRITES_TREE_RELATIONSHIPS = (
    Recipe("writes.tree.relationship_create", ("createRelationship",), "state-change",
           relationship_create),
    Recipe("writes.tree.couple_update", ("updateCoupleRelationship",), "state-change",
           couple_update),
    Recipe("writes.tree.child_and_parents_update", ("updateChildAndParentsRelationship",),
           "state-change", child_and_parents_update),
    Recipe("writes.tree.couple_delete_restore",
           ("deleteCoupleRelationship", "restoreCoupleRelationship"), "state-change",
           couple_delete_restore),
    Recipe("writes.tree.child_and_parents_delete", ("deleteChildAndParentsRelationship",),
           "state-change", child_and_parents_delete),
    Recipe("writes.tree.relationship_parent_delete",
           ("deleteChildAndParentsRelationshipParent",), "state-change",
           relationship_parent_delete),
)
