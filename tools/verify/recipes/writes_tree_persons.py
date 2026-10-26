"""Person writes: update, conclusion delete, source reference delete, memory upload.

Each recipe claims only the write it calls through ``ctx.api`` and proves it by
reading the person back (readPerson, readPersonSources, getPersonMemories: verified
reads, not claimed) before and after: the value the write should change must change
(``ctx.expect_changed``, with server-derived values on both sides) and the
conclusions it should not touch must read back as they were. Every object written is
a ``ZZSPIKE`` fixture this run created and tracked before any further call; strict
teardown deletes them (a source reference and a person memory's persona go with
their person, the memory itself through ``Memories.delete``).
"""
from familysearch.models import FACT_BIRTH, GENDER_URI, build_fact

from ..core import Recipe, VerifyContext
from ..world import PNG
from .writes_common import (FACT_RESIDENCE, describe, expect_same, fact_update,
                            gender_update, name_update, other_gender, person_memories,
                            person_sources, poll, read_person_view, token)

SURNAME = "ZZSPIKEUpdated"
PNG_NAME = "zzspike.png"


def _single(ids, what: str, view) -> str:
    if len(ids) != 1:
        raise AssertionError(f"expected exactly one {what}, found {len(ids)}: "
                             f"{view.summary()}; {describe(view.result)}")
    return next(iter(ids))


def person_update(ctx: VerifyContext) -> None:
    """Change a fixture's name, add a fact, change its gender: three updatePersons.

    Each is read back from readPerson. Negative controls: a fresh marker is absent
    from the names before the name update and present after; a fresh place marker is
    on no fact before the fact is added and on exactly one new fact after; the
    gender type differs before and after. Siblings: the name update keeps the name
    count (it updated the name by its id: no name added) and drops the old text,
    and leaves facts and gender alone; adding a fact keeps every earlier fact (by id
    and value), the names and the gender; the gender change keeps names and facts.
    """
    pid = ctx.fixtures.person(given="Update")
    v0 = read_person_view(ctx, pid)
    name_id = _single(v0.names, "name on the fixture", v0)
    old_name = v0.names[name_id]

    marker = token("n")
    ctx.api.update_person(pid, reason=ctx.reason,
                          body=name_update(marker, SURNAME, name_id=name_id))
    v1 = read_person_view(ctx, pid)
    ctx.expect_changed(v0.name_has(marker), v1.name_has(marker),
                       f"presence of name marker {marker}")
    expect_same(len(v0.names), len(v1.names), "the number of names (an update by id adds "
                "none)", v1)
    if any(old_name and old_name in text for text in v1.names.values()):
        raise AssertionError(f"the old name {old_name!r} still reads back after the name "
                             f"update: {v1.summary()}")
    expect_same(v0.facts, v1.facts, "facts (on a name update)", v1)
    expect_same(v0.gender_type, v1.gender_type, "gender (on a name update)", v1)

    place = token("f")
    ctx.api.update_person(pid, reason=ctx.reason,
                          body=fact_update(build_fact(FACT_RESIDENCE, place=place)))
    v2 = read_person_view(ctx, pid)
    ctx.expect_changed(bool(v1.facts_with(place)), bool(v2.facts_with(place)),
                       f"presence of a fact marked {place}")
    added = set(v2.facts) - set(v1.facts)
    fid = _single(added, "new fact", v2)
    if v2.facts[fid].type != FACT_RESIDENCE or v2.facts[fid].place != place:
        raise AssertionError(f"the new fact {fid} is not the Residence at {place}: "
                             f"{v2.summary()}")
    expect_same({k: v2.facts.get(k) for k in v1.facts}, dict(v1.facts),
                "the earlier facts (on adding one)", v2)
    expect_same(v1.names, v2.names, "names (on adding a fact)", v2)
    expect_same(v1.gender_type, v2.gender_type, "gender (on adding a fact)", v2)

    target = other_gender(v2.gender_type)
    ctx.api.update_person(pid, reason=ctx.reason,
                          body=gender_update(target, gender_id=v2.gender[0]))
    v3 = read_person_view(ctx, pid)
    ctx.expect_changed(v2.gender_type, v3.gender_type, "gender type")
    if v3.gender_type != GENDER_URI[target]:
        raise AssertionError(f"gender reads back {v3.gender_type!r}, not "
                             f"{GENDER_URI[target]!r}: {v3.summary()}")
    expect_same(v2.names, v3.names, "names (on a gender update)", v3)
    expect_same(v2.facts, v3.facts, "facts (on a gender update)", v3)


def person_conclusions(ctx: VerifyContext) -> None:
    """Delete one fact of a fixture by its conclusion id; read it back gone.

    The fixture is created with a birth and a Residence marked with a fresh place;
    the Residence's id is the one readPerson answers. Negative control: that id is
    among the facts before the delete and not after. Siblings: every other fact (by
    id and value), the names and the gender read back unchanged.
    """
    place = token("c")
    pid = ctx.fixtures.person(given="Conclusion",
                              facts=[build_fact(FACT_RESIDENCE, place=place)])
    v0 = read_person_view(ctx, pid)
    cid = _single(v0.facts_with(place), f"fact marked {place}", v0)
    others = {k: f for k, f in v0.facts.items() if k != cid}
    if not any(f.type == FACT_BIRTH for f in others.values()):
        raise AssertionError(f"the fixture's birth fact is missing: {v0.summary()}")
    ctx.api.delete_person_conclusion(pid, cid, reason=ctx.reason)
    v1 = read_person_view(ctx, pid)
    ctx.expect_changed(cid in v0.facts, cid in v1.facts, f"presence of fact {cid}")
    if v1.facts_with(place):
        raise AssertionError(f"a fact marked {place} still reads back: {v1.summary()}")
    expect_same(others, dict(v1.facts), "the other facts", v1)
    expect_same(v0.names, v1.names, "names", v1)
    expect_same(v0.gender_type, v1.gender_type, "gender", v1)


def person_source_reference(ctx: VerifyContext) -> None:
    """Attach two fixture sources to a fixture person, delete one reference by srid.

    The sources are ZZSPIKE descriptions attached with the proven library call
    (``Sources.attach_to_person``); their reference ids are the ones
    readPersonSources answers (polled: beta shows the attach after a lag).
    Negative control: the deleted srid is listed before and (polled until it is
    gone) not after. Sibling: the other source's reference still reads back, unchanged.
    """
    pid = ctx.fixtures.person(given="SourceRef")
    drop = ctx.fixtures.source(token("s"))
    keep = ctx.fixtures.source(token("k"))
    ctx.fixtures.attach_source(pid, drop)
    ctx.fixtures.attach_source(pid, keep)
    before = poll(ctx, lambda: person_sources(ctx, pid),
                  lambda s: {drop, keep} <= set(s.items.values()),
                  "readPersonSources: both attached sources")
    drop_refs = [r for r, sid in before.items.items() if sid == drop]
    keep_refs = {r: sid for r, sid in before.items.items() if sid == keep}
    srid = _single_ref(drop_refs, drop, before)
    ctx.api.delete_person_source_reference(pid, srid, reason=ctx.reason)
    after = poll(ctx, lambda: person_sources(ctx, pid), lambda s: srid not in s.items,
                 f"readPersonSources: reference {srid} gone")
    ctx.expect_changed(srid in before.items, srid in after.items,
                       f"presence of source reference {srid}")
    if drop in after.items.values():
        raise AssertionError(f"source {drop} is still referenced after deleting {srid}; "
                             f"{describe(after.result)}")
    kept = {r: sid for r, sid in after.items.items() if sid == keep}
    if kept != keep_refs:
        raise AssertionError(f"the other source's reference changed: {keep_refs} -> {kept}; "
                             f"{describe(after.result)}")


def _single_ref(refs, sid: str, listing) -> str:
    if len(refs) != 1:
        raise AssertionError(f"expected one reference to source {sid}, found {refs}; "
                             f"{describe(listing.result)}")
    return refs[0]


def person_memory(ctx: VerifyContext) -> None:
    """Upload a 1x1 PNG to a fixture person with createPersonMemory; read it back.

    The memory id is the upload's X-ENTITY-ID (as the library's proven upload reads
    it; the Location names the persona reference, not the memory) and is tracked
    at once, so teardown deletes it with ``Memories.delete``. Negative control: the
    id is not in getPersonMemories before the upload and (polled) is after, with the
    fresh title the upload sent. Without an X-ENTITY-ID, the memory is the one new
    entry bearing that title (tracked before anything else), or the recipe fails.
    """
    pid = ctx.fixtures.person(given="Memory")
    before = person_memories(ctx, pid)
    title = token("m")
    result = ctx.api.create_person_memory(
        pid, reason=ctx.reason, files={"artifact": (PNG_NAME, PNG, "image/png")},
        query={"filename": PNG_NAME, "title": title})
    lowered = {k.lower(): v for k, v in result.headers.items()}
    mid = lowered.get("x-entity-id")
    if mid:
        ctx.fixtures.track("memory", mid)
        after = poll(ctx, lambda: person_memories(ctx, pid), lambda m: mid in m.items,
                     f"getPersonMemories: memory {mid}")
    else:
        try:
            after = poll(ctx, lambda: person_memories(ctx, pid),
                         lambda m: any(title in t for k, t in m.items.items()
                                       if k not in before.items),
                         f"getPersonMemories: a new memory titled {title}")
        except AssertionError as e:
            raise AssertionError(f"{e}; the upload answered no X-ENTITY-ID (Location "
                                 f"{lowered.get('location')!r}): its memory may be left "
                                 "on beta") from None
        new = [k for k, t in after.items.items() if k not in before.items and title in t]
        for found in new:
            ctx.fixtures.track("memory", found)
        if len(new) != 1:
            raise AssertionError(f"no X-ENTITY-ID and {len(new)} new memories titled "
                                 f"{title}; {describe(after.result)}")
        mid = new[0]
    ctx.expect_changed(mid in before.items, mid in after.items, f"presence of memory {mid}")
    if title not in after.items[mid]:
        raise AssertionError(f"memory {mid} reads back titled {after.items[mid]}, not "
                             f"{title!r}; {describe(after.result)}")


WRITES_TREE_PERSONS = (
    Recipe("writes.tree.person_update", ("updatePerson",), "state-change", person_update),
    Recipe("writes.tree.person_conclusions", ("deletePersonConclusion",), "state-change",
           person_conclusions),
    Recipe("writes.tree.person_source_reference", ("deletePersonSourceReference",),
           "state-change", person_source_reference),
    Recipe("writes.tree.person_memory", ("createPersonMemory",), "state-change",
           person_memory),
)
