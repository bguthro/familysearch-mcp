"""Private (Genealogies) relationship writes: update (add a fact) and delete.

The only documented way to make a relationship here is updateGenealogiesTree with
``relationships`` beside ``persons`` (the spec: a couple needs ``person1`` male and
``person2`` female), done by ``FixtureSet.genealogies_couple`` (unclaimed; both persons
are LIVING ZZSPIKE fixtures, tracked at once, in a tree this run created). There is no
relationship read operation: the recipes find the relationship (its id, the persons it
joins, its facts) in the ``relationships`` of readGenealogiesPerson, which is a guess. If
that read lists none, the recipe fails with the person's skeleton, which is the evidence
for deferring these two operations. The relationship goes with its persons at teardown.
"""
from __future__ import annotations

from familysearch.models import GEDCOMX, build_fact

from ..core import Recipe, VerifyContext
from .genealogies_common import person_relationships, read_person
from .writes_common import (COUPLE, describe_box, expect_same, redacted, token)
from .writes_common import poll as _poll

FACT_MARRIAGE = GEDCOMX + "Marriage"


def _planted(ctx: VerifyContext):
    """A fixture couple: (tid, male id, female id, relationship id, its view)."""
    tid = ctx.fixtures.genealogies_tree(token("t"))
    male, female = ctx.fixtures.genealogies_couple(tid, token("c"))
    person = read_person(ctx, male)
    found = person_relationships(person)
    if len(found) != 1:
        raise AssertionError(f"person {male} lists {len(found)} relationships, wanted the "
                             f"planted couple; {describe_box(person.result)}")
    cid, rel = next(iter(found.items()))
    if rel.kind != "couple" or rel.persons != {male, female}:
        raise AssertionError(f"relationship {cid} reads back {rel.summary()}, wanted a "
                             f"{COUPLE} of {sorted({male, female})}")
    return tid, male, female, cid, rel


@redacted
def relationship_update(ctx: VerifyContext) -> None:
    """Add a fact to a fixture couple with updateGenealogiesRelationship.

    A fixture couple (see the module docstring) is posted
    ``{"relationships": [{"facts": [Marriage at a fresh place]}]}`` at
    ``/persons/{male}/relationships/{cid}``. Negative control: a fresh place marker is on
    no fact of the relationship (as readGenealogiesPerson lists it) before and on exactly
    one new fact after. Siblings: the earlier facts, the two persons it joins, and both
    persons' names and living flags read back as they were.
    """
    tid, male, female, cid, r0 = _planted(ctx)
    m0, f0 = read_person(ctx, male), read_person(ctx, female)
    place = token("m")
    ctx.api.update_genealogies_relationship(
        male, cid, reason=ctx.reason,
        body={"relationships": [{"facts": [build_fact(FACT_MARRIAGE, place=place)]}]})
    m1 = read_person(ctx, male)
    rels = person_relationships(m1)
    if cid not in rels:
        raise AssertionError(f"relationship {cid} is no longer listed on {male}; "
                             f"{describe_box(m1.result)}")
    r1 = rels[cid]
    ctx.expect_changed(bool(r0.facts_with(place)), bool(r1.facts_with(place)),
                       f"presence of a fact marked {place} on relationship {cid}")
    added = set(r1.facts["facts"]) - set(r0.facts["facts"])
    if len(added) != 1 or r1.facts["facts"][next(iter(added))].type != FACT_MARRIAGE:
        raise AssertionError(f"relationship {cid} gained {len(added)} facts, wanted one "
                             f"Marriage at {place}: {r1.summary()}")
    expect_same({k: r1.facts["facts"].get(k) for k in r0.facts["facts"]},
                dict(r0.facts["facts"]), "the earlier facts of the relationship")
    expect_same(r0.persons, r1.persons, "the persons the relationship joins")
    expect_same((m0.view.names, m0.living), (m1.view.names, m1.living), f"person {male}")
    expect_same(f0, read_person(ctx, female), f"person {female}")


@redacted
def relationship_delete(ctx: VerifyContext) -> None:
    """Delete a fixture couple with deleteGenealogiesRelationship; read it gone.

    A fixture couple is deleted at ``/persons/{male}/relationships/{cid}``.
    Negative control: the relationship id among the male's listed relationships, before (present)
    and after (absent, polled), and absent from the female's too. Siblings: both persons
    still read back, with the names and living flags they had.
    """
    tid, male, female, cid, _r0 = _planted(ctx)
    m0, f0 = read_person(ctx, male), read_person(ctx, female)
    ctx.api.delete_genealogies_relationship(male, cid, reason=ctx.reason)
    m1 = _poll(ctx, lambda: read_person(ctx, male),
               lambda p: cid not in person_relationships(p),
               f"readGenealogiesPerson: relationship {cid} gone from {male}",
               shown=describe_box)
    ctx.expect_changed(cid in person_relationships(m0), cid in person_relationships(m1),
                       f"presence of relationship {cid} on person {male}")
    f1 = read_person(ctx, female)
    if cid in person_relationships(f1):
        raise AssertionError(f"relationship {cid} is still listed on {female}; "
                             f"{describe_box(f1.result)}")
    expect_same((m0.view.names, m0.living), (m1.view.names, m1.living), f"person {male}")
    expect_same((f0.view.names, f0.living), (f1.view.names, f1.living), f"person {female}")


WRITES_GENEALOGIES_RELATIONSHIPS = (
    Recipe("writes.genealogies.relationship_update", ("updateGenealogiesRelationship",),
           "state-change", relationship_update),
    Recipe("writes.genealogies.relationship_delete", ("deleteGenealogiesRelationship",),
           "state-change", relationship_delete),
)
