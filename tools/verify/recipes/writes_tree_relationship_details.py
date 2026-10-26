"""Relationship details: source references, conclusion deletes and the order of the persons.

Every recipe builds its relationships with the proven library (``FixtureSet.couple`` /
``child_and_parents``, two-parent ones after the parents' couple, then swept for a
couple beta derived anyway) and claims only the detail writes it calls. Conclusions
to delete are planted with the verified relationship updates (not claimed). The
read-backs are verified reads: the relationship's own resource
(readCoupleRelationship / getChildAndParentsRelationship) and, for source
references, the records citing the description (readSourceReferences, which the spec
says lists persons and relationships). The relationship's ``/source-references`` and
``/sources`` reads are unverified, so they are not relied on.

An order write (the spec: "designating which existing spouse should be person1 and
which should be person2") is proven by the roles on the relationship's resource. Its
400 for persons "currently in traditional order" is ambiguous, so both persons of a
couple (both parents) have the same gender: then no traditional order applies.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from familysearch.api._runtime import ApiResult
from familysearch.models import FACT_MARRIAGE, build_fact

from ..core import Recipe, VerifyContext
from .writes_common import (Citations, Relationship, citations, describe, expect_same, poll,
                            read_child_and_parents, read_couple, token)
from .writes_tree_relationships import ADOPTIVE, FACT_DIVORCE, _expect_kind, _sweep_quietly

ENVELOPE = {"couple": "relationships", "child_and_parents": "childAndParentsRelationships"}
WHAT = {"couple": "couple", "child_and_parents": "child-and-parents"}

# Relationship detail writes no recipe claims, with the evidence why (for p3-deferred.md).
DEFERRED: dict[str, str] = {}


# ------------------------------------------------------------------ source references


@dataclass(frozen=True)
class Cited:
    """Whether a relationship cites a description, read back two ways.

    ``listed``: readSourceReferences lists the relationship among the description's
    citing records, or the relationship's resource names a reference to it.
    ``srids``: the reference ids either read names.
    """

    listed: bool
    srids: frozenset[str]
    result: ApiResult | None = field(compare=False, repr=False)


def _cited(ctx: VerifyContext, kind: str, rid: str, sid: str,
           read: Callable[[], Relationship]) -> Cited:
    found: Citations = citations(ctx, sid)
    rel = read()
    on_resource = {srid for srid, s in rel.sources.items() if s == sid}
    listed = (kind, rid) in found.refs or bool(on_resource)
    return Cited(listed, frozenset(found.refs.get((kind, rid), frozenset()) | on_resource),
                 found.result)


def _reference_id(ctx: VerifyContext, result: ApiResult, after: Cited) -> str:
    """The created reference's id: the one the read-backs name for it, else the
    Location's ``/source-references/{srid}`` (the spec: the Location names the created
    reference), else X-ENTITY-ID (which on beta has named the owner, not the part).

    The create's headers go into ``ctx.notes`` (a 201 has no body to describe).
    """
    lowered = {k.lower(): v for k, v in result.headers.items()}
    ctx.notes.append(f"source reference create answered {result.status} "
                     f"Location={lowered.get('location')!r} "
                     f"X-ENTITY-ID={lowered.get('x-entity-id')!r}")
    location = urlsplit(lowered.get("location") or "").path.rstrip("/")
    from_location = (location.rsplit("/", 1)[-1] if "/source-references/" in location
                     else None)
    if len(after.srids) == 1:
        srid = next(iter(after.srids))
        if from_location and from_location != srid:
            ctx.notes.append(f"the Location names reference {from_location}, the "
                             f"read-backs {srid}: deleting {srid}")
        return srid
    if after.srids:
        raise AssertionError(f"expected one reference to the source, the read-backs name "
                             f"{sorted(after.srids)}; {describe(after.result)}")
    srid = from_location or lowered.get("x-entity-id")
    if not srid:
        raise AssertionError(f"no reference id: the read-backs name none and the create "
                             f"answered no Location or X-ENTITY-ID; {describe(after.result)}")
    return srid


def _source_references(ctx: VerifyContext, kind: str, rid: str, read, create, delete) -> None:
    """Attach two fixture sources to ``rid`` with ``create``, delete one with ``delete``.

    ``create(body)`` posts a reference, ``delete(srid)`` deletes one, ``read()`` reads
    the relationship's resource.
    """
    what = WHAT[kind]
    drop = ctx.fixtures.source(token("s"))
    keep = ctx.fixtures.source(token("k"))
    created = {}
    for sid in (drop, keep):
        before = _cited(ctx, kind, rid, sid, read)
        if before.listed:
            raise AssertionError(f"the {what} {rid} cites the new source {sid} before any "
                                 f"attach; {describe(before.result)}")
        result = create({ENVELOPE[kind]: [{"sources": [
            {"description": ctx.fixtures.source_url(sid)}]}]})
        after = poll(ctx, lambda sid=sid: _cited(ctx, kind, rid, sid, read),
                     lambda c: c.listed, f"the {what} {rid} citing source {sid}")
        ctx.expect_changed(before.listed, after.listed,
                           f"whether the {what} {rid} cites source {sid}")
        created[sid] = (_reference_id(ctx, result, after), after)
    srid, before = created[drop]
    keep_before = _cited(ctx, kind, rid, keep, read)
    delete(srid)
    after = poll(ctx, lambda: _cited(ctx, kind, rid, drop, read), lambda c: not c.listed,
                 f"the {what} {rid} no longer citing source {drop}")
    ctx.expect_changed(before.listed, after.listed,
                       f"whether the {what} {rid} cites source {drop} (reference {srid})")
    keep_after = _cited(ctx, kind, rid, keep, read)
    expect_same((keep_before.listed, keep_before.srids), (keep_after.listed, keep_after.srids),
                f"the other source {keep}'s reference on the {what} {rid}")
    if not keep_after.listed:
        raise AssertionError(f"the {what} {rid} no longer cites the other source {keep}; "
                             f"{describe(keep_after.result)}")


def couple_source_references(ctx: VerifyContext) -> None:
    """Attach two ZZSPIKE sources to a fixture couple, then delete one reference by srid.

    Each reference is created with createCoupleRelationshipSourceReference (the proven
    person attach shape, ``{"sources": [{"description": url}]}``, in the couple's
    ``relationships`` envelope). Read-backs: readSourceReferences of the description
    lists the couple (polled: beta's source links lag), or the couple's resource names
    the reference. Negative controls: whether the couple cites each source, before
    (no) and after its create (yes); whether it cites the deleted one, before and
    after the delete (polled). Sibling: the other source is still cited, by the same
    reference ids.
    """
    husband = ctx.fixtures.person(given="RefHusband")
    wife = ctx.fixtures.person(given="RefWife", gender="Female")
    crid = ctx.fixtures.couple(husband, wife)
    api, reason = ctx.api, ctx.reason
    _source_references(
        ctx, "couple", crid, lambda: read_couple(ctx, crid),
        lambda body: api.create_couple_relationship_source_reference(
            crid, reason=reason, body=body),
        lambda srid: api.delete_couple_relationship_source_reference(
            crid, srid, reason=reason))


def child_and_parents_source_references(ctx: VerifyContext) -> None:
    """Attach two ZZSPIKE sources to a fixture child-and-parents relationship, then
    delete one reference by srid.

    One parent (beta derives no couple). As ``couple_source_references``, in the
    ``childAndParentsRelationships`` envelope; read back from readSourceReferences of
    the description and the relationship's resource. Negative controls: whether the
    relationship cites each source, before and after its create, and the deleted one
    before and after the delete. Sibling: the other source is still cited, unchanged.
    """
    parent = ctx.fixtures.person(given="RefParent")
    child = ctx.fixtures.person(given="RefChild", birth_year="1880")
    caprid = ctx.fixtures.child_and_parents(child, [parent])
    api, reason = ctx.api, ctx.reason
    _source_references(
        ctx, "child_and_parents", caprid, lambda: read_child_and_parents(ctx, caprid),
        lambda body: api.create_child_and_parents_relationship_source_reference(
            caprid, reason=reason, body=body),
        lambda srid: api.delete_child_and_parents_relationship_source_reference(
            caprid, srid, reason=reason))


# ------------------------------------------------------------------ conclusions


def _new_fact(before: Relationship, after: Relationship, key: str) -> str:
    new = set(after.facts.get(key, {})) - set(before.facts.get(key, {}))
    if len(new) != 1:
        raise AssertionError(f"expected the update to add one fact to {key}, found "
                             f"{sorted(new)}: {after.summary()}; {describe(after.result)}")
    return new.pop()


def couple_conclusion_delete(ctx: VerifyContext) -> None:
    """Delete one of two facts of a fixture couple by its conclusion id.

    A Marriage and a Divorce, each with a fresh place marker, are added with the
    verified updateCoupleRelationship (not claimed) and their ids read back from the
    couple's resource. Negative control: the Divorce's id among the couple's facts,
    before (present) and after the delete (absent). Siblings: the Marriage reads back
    unchanged (id and value), and so do the couple's persons.
    """
    husband = ctx.fixtures.person(given="ConHusband")
    wife = ctx.fixtures.person(given="ConWife", gender="Female")
    crid = ctx.fixtures.couple(husband, wife)
    v = read_couple(ctx, crid)
    _expect_kind(v, "couple", {"person1": husband, "person2": wife})
    ids = {}
    for fact_type in (FACT_MARRIAGE, FACT_DIVORCE):
        ctx.api.update_couple_relationship(crid, reason=ctx.reason, body={"relationships": [
            {"facts": [build_fact(fact_type, date="1874", place=token("w"))]}]})
        after = read_couple(ctx, crid)
        ids[fact_type] = _new_fact(v, after, "facts")
        v = after
    cid, kept = ids[FACT_DIVORCE], ids[FACT_MARRIAGE]
    ctx.api.delete_couple_relationship_conclusion(crid, cid, reason=ctx.reason)
    after = read_couple(ctx, crid)
    ctx.expect_changed(cid in v.facts["facts"], cid in after.facts.get("facts", {}),
                       f"presence of fact {cid} on couple {crid}")
    expect_same(v.facts["facts"][kept], after.facts.get("facts", {}).get(kept),
                f"the couple's other fact {kept}")
    expect_same(dict(v.roles), dict(after.roles), "the couple's persons")


def child_and_parents_conclusion_delete(ctx: VerifyContext) -> None:
    """Delete parent1's adoptive lineage fact from a fixture child-and-parents
    relationship by its conclusion id, under the role ``parent1``.

    Two parents (their couple made first); the verified
    updateChildAndParentsRelationship (not claimed) adds one ``AdoptiveParent`` fact to
    ``parent1Facts`` and one to ``parent2Facts``. Negative control: the parent1 fact's
    id in ``parent1Facts``, before (present) and after the delete (absent). Siblings:
    the parent2 fact reads back unchanged, and so do the parents and the child.
    """
    father = ctx.fixtures.person(given="ConFather")
    mother = ctx.fixtures.person(given="ConMother", gender="Female")
    child = ctx.fixtures.person(given="ConChild", birth_year="1880")
    ctx.fixtures.couple(father, mother)
    caprid = ctx.fixtures.child_and_parents(child, [father, mother])
    try:
        _delete_parent_fact(ctx, caprid, {"parent1": father, "parent2": mother,
                                          "child": child})
    finally:  # should beta derive a couple anyway, teardown deletes it too
        _sweep_quietly(ctx, [father, mother, child])


def _delete_parent_fact(ctx: VerifyContext, caprid: str, roles: dict[str, str]) -> None:
    v = read_child_and_parents(ctx, caprid)
    _expect_kind(v, "child_and_parents", roles)
    ids = {}
    for key in ("parent1Facts", "parent2Facts"):
        ctx.api.update_child_and_parents_relationship(caprid, reason=ctx.reason, body={
            "childAndParentsRelationships": [{key: [{"type": ADOPTIVE}]}]})
        after = read_child_and_parents(ctx, caprid)
        ids[key] = _new_fact(v, after, key)
        v = after
    role, cid = "parent1", ids["parent1Facts"]
    ctx.api.delete_child_and_parents_relationship_conclusion(caprid, role, cid,
                                                             reason=ctx.reason)
    after = read_child_and_parents(ctx, caprid)
    ctx.expect_changed(cid in v.facts["parent1Facts"],
                       cid in after.facts.get("parent1Facts", {}),
                       f"presence of fact {cid} in parent1Facts of {caprid}")
    expect_same(dict(v.facts["parent2Facts"]), dict(after.facts.get("parent2Facts", {})),
                f"parent2Facts of {caprid}")
    _expect_kind(after, "child_and_parents", roles)


# ------------------------------------------------------------------ order


def _order(ctx: VerifyContext, kind: str, rid: str, read, post, roles: tuple[str, str]
           ) -> tuple[Relationship, Relationship]:
    """Post the relationship's two persons in swapped ``roles``; read the swap back."""
    v0 = read()
    first, second = (v0.roles.get(r) for r in roles)
    if not (first and second) or first == second:
        raise AssertionError(f"the fixture {WHAT[kind]} {rid} does not have two persons in "
                             f"{roles}: {v0.summary()}")
    ref = ctx.fixtures.person_ref
    post({ENVELOPE[kind]: [{roles[0]: ref(second), roles[1]: ref(first)}]})
    v1 = read()
    ctx.expect_changed(tuple(v0.roles.get(r) for r in roles),
                       tuple(v1.roles.get(r) for r in roles),
                       f"the persons in {roles} of {WHAT[kind]} {rid}")
    swapped = {**v0.roles, roles[0]: second, roles[1]: first}
    if dict(v1.roles) != swapped:
        raise AssertionError(f"{WHAT[kind]} {rid} reads back {v1.summary()}, not with "
                             f"{roles} swapped ({swapped}); {describe(v1.result)}")
    return v0, v1


def couple_spouses_order(ctx: VerifyContext) -> None:
    """Swap person1 and person2 of a fixture couple of two men.

    Same gender, so no traditional (husband-first) order applies. A Marriage is added
    first with the verified updateCoupleRelationship (not claimed). The body names the
    couple's own persons in the roles wanted. Negative control: (person1, person2)
    read back from the couple's resource before and after. Siblings: the roles are
    exactly swapped (no person lost or added) and the Marriage reads back unchanged.
    """
    a = ctx.fixtures.person(given="OrdSpouseA")
    b = ctx.fixtures.person(given="OrdSpouseB")
    crid = ctx.fixtures.couple(a, b)
    ctx.api.update_couple_relationship(crid, reason=ctx.reason, body={"relationships": [
        {"facts": [build_fact(FACT_MARRIAGE, date="1874", place=token("o"))]}]})
    v0, v1 = _order(
        ctx, "couple", crid, lambda: read_couple(ctx, crid),
        lambda body: ctx.api.update_couple_relationship_spouses_order(
            crid, reason=ctx.reason, body=body), ("person1", "person2"))
    if not v0.facts.get("facts"):
        raise AssertionError(f"the planted Marriage is not on couple {crid}: "
                             f"{v0.summary()}")
    expect_same(dict(v0.facts["facts"]), dict(v1.facts.get("facts", {})),
                f"the facts of couple {crid}")


def child_and_parents_parents_order(ctx: VerifyContext) -> None:
    """Swap parent1 and parent2 of a fixture child-and-parents relationship of two women.

    Same gender, so no traditional (father-first) order applies; their couple is made
    first. Negative control: (parent1, parent2) read back from the relationship's
    resource before and after. Siblings: the roles are exactly swapped and the child
    is unchanged.
    """
    a = ctx.fixtures.person(given="OrdParentA", gender="Female")
    b = ctx.fixtures.person(given="OrdParentB", gender="Female")
    child = ctx.fixtures.person(given="OrdChild", birth_year="1880")
    ctx.fixtures.couple(a, b)
    caprid = ctx.fixtures.child_and_parents(child, [a, b])
    try:
        _order(ctx, "child_and_parents", caprid, lambda: read_child_and_parents(ctx, caprid),
               lambda body: ctx.api.update_child_and_parents_relationship_parents_order(
                   caprid, reason=ctx.reason, body=body), ("parent1", "parent2"))
    finally:  # should beta derive a couple anyway, teardown deletes it too
        _sweep_quietly(ctx, [a, b, child])


WRITES_TREE_RELATIONSHIP_DETAILS = (
    Recipe("writes.tree.couple_source_references",
           ("createCoupleRelationshipSourceReference",
            "deleteCoupleRelationshipSourceReference"), "state-change",
           couple_source_references),
    Recipe("writes.tree.child_and_parents_source_references",
           ("createChildAndParentsRelationshipSourceReference",
            "deleteChildAndParentsRelationshipSourceReference"), "state-change",
           child_and_parents_source_references),
    Recipe("writes.tree.couple_conclusion_delete", ("deleteCoupleRelationshipConclusion",),
           "state-change", couple_conclusion_delete),
    Recipe("writes.tree.child_and_parents_conclusion_delete",
           ("deleteChildAndParentsRelationshipConclusion",), "state-change",
           child_and_parents_conclusion_delete),
    Recipe("writes.tree.couple_spouses_order", ("updateCoupleRelationshipSpousesOrder",),
           "state-change", couple_spouses_order),
    Recipe("writes.tree.child_and_parents_parents_order",
           ("updateChildAndParentsRelationshipParentsOrder",), "state-change",
           child_and_parents_parents_order),
)
