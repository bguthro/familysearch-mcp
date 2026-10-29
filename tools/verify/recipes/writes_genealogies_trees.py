"""Private (Genealogies) tree writes: create, update and delete a tree.

One recipe per write, each claiming only the write it calls through ``ctx.api``. Every
tree and person written is a ZZSPIKE fixture this run created and tracked the moment its
id was known (``FixtureSet.genealogies_tree`` with the raw client, or the create's own
answer), so strict teardown deletes it: persons, then sources, then the tree. The account's
current tree and any tree it already had are never touched. A tree is only ever read by id: readGenealogiesTrees answers a list of PUBLIC collections (live),
not the user's trees, so no recipe uses it.

A tree is created with a ``sourceDescriptions`` title (live: 201, id ``2:2:2:<id>``; the
create's status and headers go into ``ctx.notes``). The spec says
updateGenealogiesTree "updates a genealogies tree with persons and/or relationships": it
does not rename, it adds, so it is proven by adding a LIVING person and reading the tree's
persons back. Ids are only ever used as the server returns them.
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from ..fixtures import create_tree, living_person_body, new_persons
from .genealogies_common import NO_SUCH_GENEALOGIES, persons, read_person, read_tree
from .writes_common import (GONE, created_id, describe_box, expect_same, redacted, status_of,
                            token)
from .writes_common import poll as _poll

SURNAME = "ZZSPIKETreeUpdate"


def poll(ctx: VerifyContext, fetch, done, what: str):
    """``writes_common.poll``, failing with the account-free skeleton."""
    return _poll(ctx, fetch, done, what, shown=describe_box)


def _own(ctx: VerifyContext, tid: str) -> None:
    if tid not in ctx.fixtures.tracked("genealogies_tree"):
        raise AssertionError(f"refusing to write tree {tid}: not created and tracked by "
                             "this run")


def _header_note(result) -> str:
    lowered = {k.lower(): v for k, v in result.headers.items()}
    return (f"answered {result.status} Location={lowered.get('location')!r} "
            f"X-ENTITY-ID={lowered.get('x-entity-id')!r} Entity-Id={lowered.get('entity-id')!r}")


@redacted
def tree_create(ctx: VerifyContext) -> None:
    """Create a ZZSPIKE private tree with createGenealogiesTree; read it back by id.

    The body is a ``sourceDescriptions`` title (the one beta took live); the id is the
    create's X-ENTITY-ID / Entity-Id / Location (``2:2:2:<id>``), tracked at once, and the
    create must answer one. Negative control: readGenealogiesTree's status for an id that
    does not exist (an error) against the new id's (200). The read must name the id with
    the marker title. (GET /genealogies/trees lists public collections, not the user's
    trees, so it proves nothing about the create.)
    """
    title = token("t")
    unknown = status_of(lambda: ctx.api.read_genealogies_tree(NO_SUCH_GENEALOGIES))
    result, shape = create_tree(
        lambda body: ctx.api.create_genealogies_tree(reason=ctx.reason, body=body), title)
    ctx.notes.append(f"tree create ({shape} body) {_header_note(result)}")
    tid = created_id(result, "tree")
    if not tid:
        raise AssertionError(f"the create answered no X-ENTITY-ID, Entity-Id or Location; "
                             f"{describe_box(result)}")
    ctx.fixtures.track("genealogies_tree", tid)
    tree = read_tree(ctx, tid)
    ctx.expect_changed(unknown, tree.result.status,
                       f"readability of tree {tid} (vs an unknown id)")
    if title not in tree.shown:
        raise AssertionError(f"tree {tid} reads back {tree.shown!r}, not titled {title!r}; "
                             f"{describe_box(tree.result)}")


@redacted
def tree_update(ctx: VerifyContext) -> None:
    """Add a LIVING fixture person to a fixture tree with updateGenealogiesTree.

    The spec: the update adds persons (and relationships) and answers new ids for the
    persons created. Two ZZSPIKE trees; only the first is updated, with
    ``{"persons": [{"id": "zz1", living: true, a marker name}]}``. Every id the answer
    names that is not the local one is tracked at once (else the person is found by the
    marker in the tree's persons and tracked). Negative control: the marker in the
    tree's persons (readGenealogiesPersons), absent before and present (polled) after,
    on exactly one person, who reads back living with that name. Siblings: the other
    tree still lists no persons, and both trees read back as they were.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    sibling = ctx.fixtures.genealogies_tree(token("s"))
    _own(ctx, tid)
    p0, s0 = persons(ctx, tid), persons(ctx, sibling)
    t0, st0 = read_tree(ctx, tid), read_tree(ctx, sibling)
    marker = token("n")
    result = ctx.api.update_genealogies_tree(
        tid, reason=ctx.reason, body=living_person_body(marker, SURNAME, "Female",
                                                        local_id="zz1"))
    ctx.notes.append(f"tree update answered {result.status}")
    for pid in new_persons(result.body, {"zz1"}):
        ctx.fixtures.track("genealogies_person", pid)
    p1 = poll(ctx, lambda: persons(ctx, tid),
              lambda p: any(marker in name for name, _ in p.items.values()),
              f"readGenealogiesPersons: a person named {marker}")
    found = [pid for pid, (name, _) in p1.items.items() if marker in name]
    for pid in found:
        if pid not in ctx.fixtures.tracked("genealogies_person"):
            ctx.fixtures.track("genealogies_person", pid)
    ctx.expect_changed(any(marker in n for n, _ in p0.items.values()),
                       any(marker in n for n, _ in p1.items.values()),
                       f"presence of a person named {marker} in tree {tid}")
    if len(found) != 1 or set(p1.items) - set(found) or p1.items[found[0]][1] is not True:
        raise AssertionError(f"tree {tid} lists persons {p1.items!r}, wanted exactly one "
                             f"living person named {marker}; {describe_box(p1.result)}")
    person = read_person(ctx, found[0])
    if person.living is not True or not person.view.name_has(marker):
        raise AssertionError(f"person {found[0]} reads back living={person.living!r}, "
                             f"names {dict(person.view.names)}; {describe_box(person.result)}")
    expect_same(s0.items, persons(ctx, sibling).items, f"the persons of the other tree {sibling}")
    expect_same(t0, read_tree(ctx, tid), f"tree {tid} itself")
    expect_same(st0, read_tree(ctx, sibling), f"the other tree {sibling}")


@redacted
def tree_delete(ctx: VerifyContext) -> None:
    """Delete a fixture tree with deleteGenealogiesTree; read it back gone.

    Two empty ZZSPIKE trees; only the first is deleted (an empty tree, so a cascade to
    persons cannot hide the proof). Negative control: readGenealogiesTree's status for it,
    before (200) and after (404/410); only then is it marked deleted. Sibling: the other
    tree still reads back as it was.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    sibling = ctx.fixtures.genealogies_tree(token("s"))
    _own(ctx, tid)
    before = read_tree(ctx, tid).result.status
    s0 = read_tree(ctx, sibling)
    ctx.api.delete_genealogies_tree(tid, reason=ctx.reason)
    after = status_of(lambda: ctx.api.read_genealogies_tree(tid))
    ctx.expect_changed(before, after, f"readability of tree {tid}")
    if after not in GONE:
        raise AssertionError(f"deleted tree {tid} reads back {after}, not 404/410")
    ctx.fixtures.mark_deleted("genealogies_tree", tid)
    expect_same(s0, read_tree(ctx, sibling), f"the other tree {sibling}")


WRITES_GENEALOGIES_TREES = (
    Recipe("writes.genealogies.tree_create", ("createGenealogiesTree",), "state-change",
           tree_create),
    Recipe("writes.genealogies.tree_update", ("updateGenealogiesTree",), "state-change",
           tree_update),
    Recipe("writes.genealogies.tree_delete", ("deleteGenealogiesTree",), "state-change",
           tree_delete),
)
