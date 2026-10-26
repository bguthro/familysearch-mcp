"""Memory links: personas, a person's memory references, and portraits.

A memory is linked to a tree person by a persona (id ``{memory id}-{n}``); these recipes
add, rename and remove those links, and select or clear a person's portrait. Every
check is on the exact link sets (which persona links which person, whose memory list
holds the memory, which portraits a person has), never on a 2xx: siblings, the other
persons' links and the memory itself must read back unchanged.

Only recipe-created ZZSPIKE memories (tiny generated PNGs through ``FixtureSet.memory``,
torn down with the proven ``Memories.delete``, which takes their personas with them) and
deceased ZZSPIKE persons are written. A fixture link is planted with
``FixtureSet.link_memory`` (the proven ``Memories.link_to_person`` body) and a fixture
portrait with ``FixtureSet.set_portrait``; neither is claimed. The read-backs are the
verified readMemoryPersonas / readMemoryPersona / getPersonMemories / readMemory and the
unverified readPersonPortraits, all unclaimed. A persona is addressed by the id
readMemoryPersonas answers (the form ``{memory id}-{n}``, which is also the id in the
Location of the person-memory upload: the reference recipe relies on that equality).
Failure text drops links and contributors (``describe_box``).
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from ..fixtures import portrait_body
from .writes_common import (GONE, describe_box, expect_same, person_memories, redacted,
                            read_person_view, status_of, token)
from .writes_memory_common import (exactly, link_sets, own, persona_link_body,
                                   persona_name_body, persona_of, personas_listing,
                                   poll_persona, poll_personas, poll_person_memories,
                                   poll_portraits, portrait_titles, read_memory,
                                   read_persona)

DEFERRED: dict[str, str] = {}


def _linked(ctx: VerifyContext, given: str):
    """A memory uploaded to one fixture person and linked to a second: returns
    ``(owner, other, mid, personas)`` once beta lists both personas and both memory lists."""
    owner = ctx.fixtures.person(given=given + "A")
    other = ctx.fixtures.person(given=given + "B")
    mid = ctx.fixtures.memory(token("m"), person=owner)
    ctx.fixtures.link_memory(mid, other)
    own(ctx, "memory", mid)
    personas = poll_personas(ctx, mid, lambda p: len(p.items) >= 2,
                             f"readMemoryPersonas: both personas of memory {mid}")
    if link_sets(personas, [owner, other]) != sorted([(owner,), (other,)]):
        raise AssertionError(f"memory {mid} has personas linking "
                             f"{link_sets(personas, [owner, other])}, not exactly {owner} and "
                             f"{other}; {describe_box(personas.result)}")
    for pid in (owner, other):
        poll_person_memories(ctx, pid, lambda m: mid in m.items,
                             f"getPersonMemories: {mid} for {pid}")
    return owner, other, mid, personas


def _memory_lists(ctx: VerifyContext, mid: str, with_memory: list[str], without: list[str],
                  what: str) -> None:
    for pid in with_memory:
        found = poll_person_memories(ctx, pid, lambda m: mid in m.items,
                                     f"getPersonMemories: {mid} for {pid}")
        if set(found.items) != {mid}:
            raise AssertionError(f"{what}: the memories of {pid} are {sorted(found.items)}, "
                                 f"not exactly {mid}; {describe_box(found.result)}")
    for pid in without:
        found = person_memories(ctx, pid)
        if found.items:
            raise AssertionError(f"{what}: the memories of {pid} are {sorted(found.items)}, "
                                 f"not none; {describe_box(found.result)}")


@redacted
def persona_create(ctx: VerifyContext) -> None:
    """Link a fixture memory to a second fixture person with createMemoryPersona.

    A memory is uploaded to the first person (so it has one persona); a third person is
    never linked. The claimed POST is the proven link body (the memory's URL and the
    person's ark). Negative control: the memory among the second person's memories
    (getPersonMemories), before (absent) and after (present, polled). The personas
    (readMemoryPersonas with tree identifiers) must then link exactly the first and the
    second person, the created persona id (the response's X-ENTITY-ID, where answered)
    among them linking the second; the first person's list still holds exactly the
    memory, the second's exactly its own other memory and this one, the third person's is
    still empty.
    """
    first = ctx.fixtures.person(given="PersonaCreateA")
    second = ctx.fixtures.person(given="PersonaCreateB")
    third = ctx.fixtures.person(given="PersonaCreateC")
    mid = ctx.fixtures.memory(token("m"), person=first)
    kept = ctx.fixtures.memory(token("k"), person=second)
    own(ctx, "memory", mid)
    pids = [first, second, third]
    p0 = poll_personas(ctx, mid, lambda p: len(p.items) >= 1,
                       f"readMemoryPersonas: the persona of memory {mid}")
    if link_sets(p0, pids) != [(first,)]:
        raise AssertionError(f"memory {mid} has personas linking {link_sets(p0, pids)}, not "
                             f"exactly {first}; {describe_box(p0.result)}")
    l0 = poll_person_memories(ctx, second, lambda m: kept in m.items,
                              f"getPersonMemories: {kept} for {second}")
    exactly(l0, {kept}, f"the memories of {second} before the link")
    result = ctx.api.create_memory_persona(
        mid, reason=ctx.reason, body=persona_link_body(
            ctx.fixtures.memory_url(mid), ctx.client.base_url, second))
    l1 = poll_person_memories(ctx, second, lambda m: mid in m.items,
                              f"getPersonMemories: {mid} for {second}")
    ctx.expect_changed(mid in l0.items, mid in l1.items,
                       f"presence of memory {mid} among the memories of {second}")
    exactly(l1, {kept, mid}, f"the memories of {second} after the link")
    p1 = poll_personas(ctx, mid, lambda p: len(p.items) >= 2,
                       f"readMemoryPersonas: both personas of memory {mid}")
    if link_sets(p1, pids) != sorted([(first,), (second,)]):
        raise AssertionError(f"memory {mid} has personas linking {link_sets(p1, pids)}, not "
                             f"exactly {first} and {second}; {describe_box(p1.result)}")
    created = result.entity_id
    if created and created in p1.items and p1.items[created].links(pids) != (second,):
        raise AssertionError(f"the created persona {created} links "
                             f"{p1.items[created].links(pids)}, not {second}; "
                             f"{describe_box(p1.result)}")
    _memory_lists(ctx, mid, [first], [third], "after the link")
    expect_same(l0.items[kept], l1.items[kept], f"the title of {kept} in {second}'s list")


@redacted
def persona_update(ctx: VerifyContext) -> None:
    """Rename a memory persona with updateMemoryPersona.

    A memory uploaded to the first fixture person and linked to the second has two
    personas; only the first person's is posted, by its id, with a new name (given name
    a fresh marker, with name parts). Negative control: the persona's names
    (readMemoryPersona), before and after (polled): the marker is in the names only
    after. The tree person is not renamed (readPerson names unchanged); the second
    persona reads back as it was; the personas still link exactly the two persons and
    both persons' memory lists still hold exactly the memory.
    """
    owner, other, mid, personas = _linked(ctx, "PersonaUpdate")
    pids = [owner, other]
    persona_id, other_id = persona_of(personas, owner), persona_of(personas, other)
    n0, o0 = read_persona(ctx, mid, persona_id), read_persona(ctx, mid, other_id)
    v0 = read_person_view(ctx, owner)
    given = token("n")
    ctx.api.update_memory_persona(mid, persona_id, reason=ctx.reason,
                                  body=persona_name_body(persona_id, given))
    n1 = poll_persona(ctx, mid, persona_id, lambda p: any(given in n for n in p.names),
                      f"readMemoryPersona: persona {persona_id} renamed")
    ctx.expect_changed(sorted(n0.names), sorted(n1.names), f"names of persona {persona_id}")
    expect_same(o0, read_persona(ctx, mid, other_id), f"the other persona {other_id}")
    expect_same(v0.names, read_person_view(ctx, owner).names,
                f"the tree names of {owner} (a persona rename leaves them)")
    after = personas_listing(ctx, mid)
    if link_sets(after, pids) != sorted([(owner,), (other,)]):
        raise AssertionError(f"memory {mid} has personas linking {link_sets(after, pids)}, "
                             f"not exactly {owner} and {other}; {describe_box(after.result)}")
    _memory_lists(ctx, mid, pids, [], "after the rename")


def _unlink(ctx: VerifyContext, given: str, remove) -> None:
    """The shared check of the two link removals: ``remove(owner, other, mid, persona_id)``
    sends the claimed delete for the second person's persona."""
    owner, other, mid, personas = _linked(ctx, given)
    pids = [owner, other]
    persona_id = persona_of(personas, other)
    kept_id = persona_of(personas, owner)
    m0, l0 = read_memory(ctx, mid), person_memories(ctx, other)
    remove(owner, other, mid, persona_id)
    after = poll_personas(ctx, mid, lambda p: persona_id not in p.items,
                          f"readMemoryPersonas: persona {persona_id} gone")
    l1 = poll_person_memories(ctx, other, lambda m: mid not in m.items,
                              f"getPersonMemories: {mid} gone for {other}")
    ctx.expect_changed(persona_id in personas.items, persona_id in after.items,
                       f"presence of persona {persona_id}")
    ctx.expect_changed(mid in l0.items, mid in l1.items,
                       f"presence of memory {mid} among the memories of {other}")
    if set(after.items) != {kept_id} or link_sets(after, pids) != [(owner,)]:
        raise AssertionError(f"memory {mid} has personas {sorted(after.items)} linking "
                             f"{link_sets(after, pids)}, not exactly {kept_id} linking "
                             f"{owner}; {describe_box(after.result)}")
    exactly(poll_person_memories(ctx, owner, lambda m: mid in m.items,
                                 f"getPersonMemories: {mid} for {owner}"), {mid},
            f"the memories of {owner} after the removal")
    exactly(l1, set(), f"the memories of {other} after the removal")
    status = status_of(lambda: ctx.api.read_memory(mid))
    if status in GONE:
        raise AssertionError(f"removing a link deleted memory {mid} itself (it reads back "
                             f"{status})")
    expect_same(m0, read_memory(ctx, mid), f"memory {mid}")


@redacted
def persona_delete(ctx: VerifyContext) -> None:
    """Remove a memory's link to a person with deleteMemoryPersona.

    A memory uploaded to the first fixture person and linked to the second has two
    personas; the second's is deleted by its id. Negative control: the persona id among
    readMemoryPersonas, before (present) and after (absent, polled); and the memory among
    the second person's memories (getPersonMemories), polled. The personas must then be
    exactly the first person's, the first person's list still exactly the memory, the
    second's empty, and the memory itself must read back as it was (not deleted).
    """
    _unlink(ctx, "PersonaDelete", lambda owner, other, mid, persona_id:
            ctx.api.delete_memory_persona(mid, persona_id, reason=ctx.reason))


@redacted
def reference_delete(ctx: VerifyContext) -> None:
    """Remove a person's memory reference with deletePersonMemoriesPersonaReference.

    The same setup as deleteMemoryPersona, but the second person's link is removed from
    the person's side: ``DELETE /tree/persons/{pid}/memory-references/{erid}`` with the
    persona id as erid (the id of the Location the person-memory upload answers, which
    readMemoryPersonas also lists). Negative control, link sets and the intact memory as
    for deleteMemoryPersona.
    """
    _unlink(ctx, "ReferenceDelete", lambda owner, other, mid, persona_id:
            ctx.api.delete_person_memories_persona_reference(other, persona_id,
                                                             reason=ctx.reason))


@redacted
def portraits_update(ctx: VerifyContext) -> None:
    """Select a fixture person's portrait with updatePersonPortraits.

    A fixture person has two memories (titled with fresh markers) and a second fixture
    person none; the first memory is posted as the portrait, as a SourceDescription list
    naming the memory (the spec has no example). Negative control: the person's
    portraits (readPersonPortraits, unclaimed and not yet verified), before (none) and
    after (polled until the first memory's title shows). They must then be exactly that
    one description, never the other memory's; the second person still has none; the
    first person's memory list still holds both memories.
    """
    owner = ctx.fixtures.person(given="PortraitSet")
    bystander = ctx.fixtures.person(given="PortraitSetB")
    chosen_title, other_title = token("p"), token("q")
    chosen = ctx.fixtures.memory(chosen_title, person=owner)
    other = ctx.fixtures.memory(other_title, person=owner)
    own(ctx, "memory", chosen)
    poll_person_memories(ctx, owner, lambda m: {chosen, other} <= set(m.items),
                         f"getPersonMemories: {chosen} and {other}")
    before = poll_portraits(ctx, owner, lambda p: True, "")
    ctx.api.update_person_portraits(owner, reason=ctx.reason, body=portrait_body(
        ctx.fixtures.memory_url(chosen), chosen))
    after = poll_portraits(ctx, owner, lambda p: chosen_title in portrait_titles(p),
                           f"readPersonPortraits: portrait of {owner}")
    ctx.expect_changed(portrait_titles(before), portrait_titles(after),
                       f"portraits of {owner}")
    if portrait_titles(after) != [chosen_title]:
        raise AssertionError(f"the portraits of {owner} are titled {portrait_titles(after)}, "
                             f"not exactly {chosen_title!r}; {describe_box(after.result)}")
    bystander_after = poll_portraits(ctx, bystander, lambda p: True, "")
    if bystander_after.items:
        raise AssertionError(f"the portrait was set on another person too: {bystander} has "
                             f"{portrait_titles(bystander_after)}; "
                             f"{describe_box(bystander_after.result)}")
    exactly(poll_person_memories(ctx, owner, lambda m: {chosen, other} <= set(m.items),
                                 f"getPersonMemories: {chosen} and {other}"), {chosen, other},
            f"the memories of {owner} after the portrait")


@redacted
def portrait_delete(ctx: VerifyContext) -> None:
    """Clear a fixture person's portrait with deletePersonPortrait.

    Two fixture persons each get a memory selected as their portrait with
    ``FixtureSet.set_portrait`` (the same body updatePersonPortraits sends; not claimed);
    only the first person's is deleted. Negative control: the first person's portraits
    (readPersonPortraits), before (its memory's title, polled) and after (none, polled).
    The second person's portrait must read back as it was; the first person's memory
    list and the memory itself are untouched (the spec: only the portrait selection is
    removed; a default another user chose may still show, but this account has no
    other user).
    """
    first = ctx.fixtures.person(given="PortraitDeleteA")
    second = ctx.fixtures.person(given="PortraitDeleteB")
    first_title, second_title = token("p"), token("q")
    mine = ctx.fixtures.memory(first_title, person=first)
    theirs = ctx.fixtures.memory(second_title, person=second)
    ctx.fixtures.set_portrait(first, mine)
    ctx.fixtures.set_portrait(second, theirs)
    f0 = poll_portraits(ctx, first, lambda p: first_title in portrait_titles(p),
                        f"readPersonPortraits: portrait of {first}")
    s0 = poll_portraits(ctx, second, lambda p: second_title in portrait_titles(p),
                        f"readPersonPortraits: portrait of {second}")
    m0 = read_memory(ctx, mine)
    ctx.api.delete_person_portrait(first, reason=ctx.reason)
    f1 = poll_portraits(ctx, first, lambda p: not p.items,
                        f"readPersonPortraits: portrait of {first} gone")
    ctx.expect_changed(portrait_titles(f0), portrait_titles(f1), f"portraits of {first}")
    s1 = poll_portraits(ctx, second, lambda p: True, "")
    expect_same(portrait_titles(s0), portrait_titles(s1), f"the portraits of {second}")
    exactly(poll_person_memories(ctx, first, lambda m: mine in m.items,
                                 f"getPersonMemories: {mine} for {first}"), {mine},
            f"the memories of {first} after the delete")
    expect_same(m0, read_memory(ctx, mine), f"memory {mine}")


WRITES_MEMORY_LINKS = (
    Recipe("writes.memories.persona_create", ("createMemoryPersona",), "state-change",
           persona_create),
    Recipe("writes.memories.persona_update", ("updateMemoryPersona",), "state-change",
           persona_update),
    Recipe("writes.memories.persona_delete", ("deleteMemoryPersona",), "state-change",
           persona_delete),
    Recipe("writes.memories.reference_delete", ("deletePersonMemoriesPersonaReference",),
           "state-change", reference_delete),
    Recipe("writes.memories.portraits_update", ("updatePersonPortraits",), "state-change",
           portraits_update),
    Recipe("writes.memories.portrait_delete", ("deletePersonPortrait",), "state-change",
           portrait_delete),
)
