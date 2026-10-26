"""Memory writes: create, update, delete, and comments on a memory.

Each recipe claims only the write(s) it calls through ``ctx.api`` and proves it by
reading the memory (readMemory, verified), a person's memory list (getPersonMemories,
verified) or its comments (readMemoryComments, unclaimed until its own read recipe runs
live) back, before and after. Memories are shared artifacts: only ZZSPIKE memories this
run created and tracked are written (``FixtureSet.memory``: a tiny generated PNG through
the proven ``Memories`` upload; the claimed create makes one the same way and tracks it
the moment its id is known), and strict teardown deletes every one with the proven
``Memories.delete`` (its comments and personas go with it). Fixture persons are
deceased ZZSPIKE ones. Failure text drops links and contributors (``describe_box``).

Request bodies the spec gives no example of (updateMemory's description, the comment
discussion) are in ``writes_memory_common`` / ``tools/verify/fixtures``.
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from ..fixtures import PNG, PNG_NAME, comments_body
from .writes_common import (GONE, created_id, describe_box, expect_same, person_memories,
                            redacted, status_of, token)
from .writes_memory_common import (NO_SUCH_MEMORY, NOT_FOUND, comment_id, comments_listing, exactly,
                                   memory_body, one_description_id, own, poll_comments,
                                   poll_memory, poll_person_memories, poll_status_gone,
                                   read_memory, unknown_memory_status)

DEFERRED: dict[str, str] = {
    "updateMemoryArtifact": (
        "the spec says only story text can be updated, so a fixture needs a text/plain "
        "story memory, which the proven library refuses to upload (image and pdf only) and "
        "no verified create has made; its aid is not documented to equal the memory id, and "
        "no catalogued operation reads a story's text back, so no state-change evidence "
        "exists for it"),
    "deleteMemoryArtifactCoverage": (
        "coverage is the artifact's date and place, but no operation in the spec sets it "
        "(updateMemory documents titles and descriptions only) so a fresh fixture has none "
        "and deleting nothing changes nothing; no honest negative control, and the aid is "
        "not documented to equal the memory id"),
}


@redacted
def memory_create(ctx: VerifyContext) -> None:
    """Create a standalone ZZSPIKE memory with createMemories; read it back.

    The tiny generated PNG is uploaded as multipart (``artifact``) with a fresh title and
    description in the query; the id is the create's X-ENTITY-ID (Entity-Id, else the
    Location's last segment) and is tracked at once. Negative control: a fresh title
    marker among the titles the server answers for every memory that could carry it (the
    sibling fixture memory and the fixture person's memory list) before, and in
    readMemory of the new id after (polled). Its title and description must read back
    exactly, the media type still a PNG. It must be floating: the fixture person's memory
    list is unchanged. Sibling: the other memory reads back as it was. A made-up memory
    id reads 400/404/410 (the read is not a catch-all).
    """
    owner = ctx.fixtures.person(given="MemoryCreate")
    sibling = ctx.fixtures.memory(token("g"))
    s0, o0 = read_memory(ctx, sibling), person_memories(ctx, owner)
    title, description = token("m"), token("d")
    before = any(title in t for t in (*s0.titles, *(x for v in o0.items.values() for x in v)))
    result = ctx.api.create_memories(
        reason=ctx.reason, files={"artifact": (PNG_NAME, PNG, "image/png")},
        query={"filename": PNG_NAME, "title": title, "description": description})
    mid = created_id(result, "memory")
    if not mid:
        raise AssertionError("the memory create answered no X-ENTITY-ID, Entity-Id or "
                             f"Location, so the memory cannot be tracked and may be left on "
                             f"beta; {describe_box(result)}")
    ctx.fixtures.track("memory", mid)
    after = poll_memory(ctx, mid, lambda v: title in v.titles, f"readMemory: memory {mid}")
    ctx.expect_changed(before, title in after.titles, f"presence of a memory titled {title}")
    if after.titles != (title,) or after.descriptions != (description,):
        raise AssertionError(f"memory {mid} reads back titles {list(after.titles)} and "
                             f"descriptions {list(after.descriptions)}, not {title!r} / "
                             f"{description!r}; {describe_box(after.result)}")
    if set(after.media) - {"image/png"}:
        raise AssertionError(f"memory {mid} reads back media {list(after.media)}, not a PNG; "
                             f"{describe_box(after.result)}")
    expect_same(o0.items, person_memories(ctx, owner).items,
                f"the memories of {owner} (a created memory is floating)")
    expect_same(s0, read_memory(ctx, sibling), f"the other memory {sibling}")
    unknown = unknown_memory_status(ctx)
    if unknown not in NOT_FOUND:
        raise AssertionError(f"reading memory {NO_SUCH_MEMORY} answered {unknown}, not one of "
                             f"{list(NOT_FOUND)}")



@redacted
def memory_update(ctx: VerifyContext) -> None:
    """Change the title and description of a fixture memory with updateMemory.

    Two ZZSPIKE memories are uploaded to one fixture person; only the first is updated,
    with the description id readMemory answered, a fresh title and a fresh description.
    Negative control: the memory's titles, read back with readMemory, before and after
    (polled); they must be exactly the new title (nothing prepended or merged) and the
    descriptions exactly the new one, ``about`` and media type unchanged. Links: the
    person's memory list (polled) holds the same two memories, the first under its new
    title. Sibling: the other memory reads back as it was.
    """
    pid = ctx.fixtures.person(given="MemoryUpdate")
    mid = ctx.fixtures.memory(token("u"), person=pid)
    sibling = ctx.fixtures.memory(token("k"), person=pid)
    own(ctx, "memory", mid)
    m0, s0 = read_memory(ctx, mid), read_memory(ctx, sibling)
    l0 = poll_person_memories(ctx, pid, lambda m: {mid, sibling} <= set(m.items),
                              f"getPersonMemories: {mid} and {sibling}")
    exactly(l0, {mid, sibling}, f"the memories of {pid} before the update")
    title, description = token("t"), token("e")
    ctx.api.update_memory(mid, reason=ctx.reason, body=memory_body(
        one_description_id(m0, f"memory {mid}"), title, description))
    m1 = poll_memory(ctx, mid, lambda v: v.titles == (title,),
                     f"readMemory: memory {mid} retitled")
    ctx.expect_changed(m0.titles, m1.titles, f"title of memory {mid}")
    if m1.descriptions != (description,):
        raise AssertionError(f"memory {mid} reads back descriptions {list(m1.descriptions)}, "
                             f"not exactly {description!r}; {describe_box(m1.result)}")
    expect_same((m0.about, m0.media), (m1.about, m1.media),
                f"the about and media type of memory {mid}")
    l1 = poll_person_memories(ctx, pid, lambda m: m.items.get(mid) == (title,),
                              f"getPersonMemories: {mid} titled {title}")
    exactly(l1, {mid, sibling}, f"the memories of {pid} after the update")
    expect_same(l0.items[sibling], l1.items[sibling], f"the title of {sibling} in {pid}'s list")
    expect_same(s0, read_memory(ctx, sibling), f"the other memory {sibling}")


@redacted
def memory_delete(ctx: VerifyContext) -> None:
    """Delete a fixture memory with deleteMemory; read it back gone.

    Two ZZSPIKE memories are uploaded to one fixture person; only the first is deleted.
    Negative control: readMemory's status for it, before (200) and after (404/410,
    polled); only then is it marked deleted, so teardown skips it. Links: the person's
    memory list (polled) is then exactly the other memory. Sibling: the other memory
    reads back as it was.
    """
    pid = ctx.fixtures.person(given="MemoryDelete")
    mid = ctx.fixtures.memory(token("d"), person=pid)
    sibling = ctx.fixtures.memory(token("k"), person=pid)
    own(ctx, "memory", mid)
    s0 = read_memory(ctx, sibling)
    l0 = poll_person_memories(ctx, pid, lambda m: {mid, sibling} <= set(m.items),
                              f"getPersonMemories: {mid} and {sibling}")
    exactly(l0, {mid, sibling}, f"the memories of {pid} before the delete")
    before = status_of(lambda: ctx.api.read_memory(mid))
    ctx.api.delete_memory(mid, reason=ctx.reason)
    after = poll_status_gone(ctx, lambda: status_of(lambda: ctx.api.read_memory(mid)),
                             f"readMemory: memory {mid} gone")
    ctx.expect_changed(before, after, f"readability of memory {mid}")
    if after not in GONE:
        raise AssertionError(f"deleted memory {mid} reads back {after}, not 404/410")
    ctx.fixtures.mark_deleted("memory", mid)
    l1 = poll_person_memories(ctx, pid, lambda m: mid not in m.items,
                              f"getPersonMemories: memory {mid} gone")
    exactly(l1, {sibling}, f"the memories of {pid} after the delete")
    expect_same(s0, read_memory(ctx, sibling), f"the other memory {sibling}")


@redacted
def comment_create(ctx: VerifyContext) -> None:
    """Add a comment to a fixture memory with createMemoryComments; read it back.

    A first comment is planted with ``FixtureSet.memory_comment`` (not claimed); the claimed
    create posts one more, as ``{"discussions": [{"comments": [{"text": marker}]}]}``
    (the shape readMemoryComments answers; the spec has no example). Negative control:
    the comment texts (readMemoryComments), before (the planted one only, polled) and
    after (polled): the marker text is absent before and present once after, under a new
    comment id. The comments are then exactly the planted one (same id, same text) and the
    new one.
    """
    mid = ctx.fixtures.memory(token("c"))
    own(ctx, "memory", mid)
    planted = token("p")
    ctx.fixtures.memory_comment(mid, planted)
    c0 = poll_comments(ctx, mid, lambda c: planted in c.items.values(),
                       f"readMemoryComments: {planted} planted")
    planted_id = comment_id(c0, planted)
    text = token("x")
    ctx.api.create_memory_comments(mid, reason=ctx.reason, body=comments_body(text))
    c1 = poll_comments(ctx, mid, lambda c: text in c.items.values(),
                       f"readMemoryComments: {text}")
    ctx.expect_changed(text in c0.items.values(), text in c1.items.values(),
                       f"presence of a comment reading {text}")
    new_id = comment_id(c1, text)
    if new_id == planted_id or c1.items != {planted_id: planted, new_id: text}:
        raise AssertionError(f"memory {mid} holds comments {c1.items}, not exactly the planted "
                             f"{planted_id} and the new {new_id}; {describe_box(c1.result)}")


@redacted
def comment_delete(ctx: VerifyContext) -> None:
    """Delete one of two comments of a fixture memory with deleteMemoryComment.

    Both comments are planted with ``FixtureSet.memory_comment`` (not claimed) and their
    ids read from readMemoryComments by text; only the first is deleted. Negative control:
    its id among the memory's comments, before (present, polled) and after (absent,
    polled). The comments are then exactly the other one, same id and text.
    """
    mid = ctx.fixtures.memory(token("c"))
    own(ctx, "memory", mid)
    doomed, kept = token("a"), token("b")
    ctx.fixtures.memory_comment(mid, doomed)
    ctx.fixtures.memory_comment(mid, kept)
    c0 = poll_comments(ctx, mid, lambda c: {doomed, kept} <= set(c.items.values()),
                       f"readMemoryComments: {doomed} and {kept} planted")
    doomed_id, kept_id = comment_id(c0, doomed), comment_id(c0, kept)
    ctx.api.delete_memory_comment(mid, doomed_id, reason=ctx.reason)
    c1 = poll_comments(ctx, mid, lambda c: doomed_id not in c.items,
                       f"readMemoryComments: comment {doomed_id} gone")
    ctx.expect_changed(doomed_id in c0.items, doomed_id in c1.items,
                       f"presence of comment {doomed_id}")
    if c1.items != {kept_id: kept}:
        raise AssertionError(f"memory {mid} holds comments {c1.items}, not exactly the other "
                             f"{kept_id}; {describe_box(c1.result)}")
    expect_same(comments_listing(ctx, mid).items, c1.items, f"the comments of {mid}")


WRITES_MEMORIES = (
    Recipe("writes.memories.memory_create", ("createMemories",), "state-change",
           memory_create),
    Recipe("writes.memories.memory_update", ("updateMemory",), "state-change", memory_update),
    Recipe("writes.memories.memory_delete", ("deleteMemory",), "state-change", memory_delete),
    Recipe("writes.memories.comment_create", ("createMemoryComments",), "state-change",
           comment_create),
    Recipe("writes.memories.comment_delete", ("deleteMemoryComment",), "state-change",
           comment_delete),
)
