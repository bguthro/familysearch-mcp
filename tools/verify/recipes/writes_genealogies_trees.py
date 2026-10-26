"""Private (Genealogies) tree writes: create, update and delete a tree.

One recipe per write, each claiming only the write it calls through ``ctx.api``. Every
tree and person written is a ZZSPIKE fixture this run created and tracked the moment its
id was known (``FixtureSet.genealogies_tree`` with the raw client, or the create's own
answer), so strict teardown deletes it: persons, then sources, then the tree. The account's
current tree and any tree it already had are never touched: each recipe reads the user's
tree list (readGenealogiesTrees, unclaimed: it takes no parameter, so no not-found control
exists) before and after and the new entries are only the intended ones.

The request bodies are unproven. A tree is created with a GEDCOM X ``collections`` title
(else, if beta refuses that 400, a ``sourceDescriptions`` title: ``create_tree`` tries both,
and the create's status, Location and X-ENTITY-ID go into ``ctx.notes``). The spec says
updateGenealogiesTree "updates a genealogies tree with persons and/or relationships": it
does not rename, it adds, so it is proven by adding a LIVING person and reading the tree's
persons back. Ids are only ever used as the server returns them.
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from ..fixtures import create_tree, living_person_body, new_persons
from .genealogies_common import persons, read_person, read_tree, trees
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
    """Create a ZZSPIKE private tree with createGenealogiesTree; read it back.

    The body is a ``collections`` title (else a ``sourceDescriptions`` one if beta
    refuses that 400); the id is the create's X-ENTITY-ID / Entity-Id / Location, tracked
    at once, else the one new tree bearing the marker (every such tree tracked).
    Negative control: the new id among the user's trees (readGenealogiesTrees), before
    (absent) and after (polled). The list must name it with the marker title and show no
    other new tree (a stray bearing our marker is tracked for teardown and fails the
    recipe; a foreign one fails it but is left alone), and readGenealogiesTree must
    answer the title.
    """
    title = token("t")
    before = trees(ctx)
    if any(title in t for t in before.items.values()):
        raise AssertionError(f"a tree is titled {title} before the create")
    result, shape = create_tree(
        lambda body: ctx.api.create_genealogies_tree(reason=ctx.reason, body=body), title)
    ctx.notes.append(f"tree create ({shape} body) {_header_note(result)}")
    tid = created_id(result, "tree")
    if tid:
        ctx.fixtures.track("genealogies_tree", tid)
    after = poll(ctx, lambda: trees(ctx),
                 lambda t: (tid in t.items) if tid else any(
                     title in v for k, v in t.items.items() if k not in before.items),
                 f"readGenealogiesTrees: the tree {tid or 'titled ' + title}")
    new = [k for k in after.items if k not in before.items]
    ours = [k for k in new if title in after.items[k]]
    if not tid:
        for found in ours:
            ctx.fixtures.track("genealogies_tree", found)
        if len(ours) != 1:
            raise AssertionError(f"the create answered no id and {len(ours)} new trees are "
                                 f"titled {title}; {describe_box(after.result)}")
        tid = ours[0]
        ctx.notes.append(f"tree create answered no id: {tid} is the one new tree titled {title}")
    stray = sorted(set(new) - {tid})
    for found in stray:
        if found in ours:
            ctx.fixtures.track("genealogies_tree", found)
    if stray:
        raise AssertionError(f"new tree(s) {stray} appeared besides {tid}; "
                             f"{describe_box(after.result)}")
    ctx.expect_changed(tid in before.items, tid in after.items,
                       f"presence of tree {tid} among the user's trees")
    if title not in after.items[tid]:
        raise AssertionError(f"tree {tid} is listed as {after.items[tid]!r}, not titled "
                             f"{title!r}; {describe_box(after.result)}")
    tree = read_tree(ctx, tid)
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
    persons cannot hide the proof). Negative control: its id among the user's trees
    (readGenealogiesTrees), before (present) and after (absent, polled);
    readGenealogiesTree must then answer 404/410. Only then is it marked deleted. Sibling:
    the other tree is still listed and reads back as it was.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    sibling = ctx.fixtures.genealogies_tree(token("s"))
    _own(ctx, tid)
    read_tree(ctx, tid)
    s0, l0 = read_tree(ctx, sibling), trees(ctx)
    ctx.api.delete_genealogies_tree(tid, reason=ctx.reason)
    l1 = poll(ctx, lambda: trees(ctx), lambda t: tid not in t.items,
              f"readGenealogiesTrees: tree {tid} gone")
    ctx.expect_changed(tid in l0.items, tid in l1.items,
                       f"presence of tree {tid} among the user's trees")
    after = status_of(lambda: ctx.api.read_genealogies_tree(tid))
    if after not in GONE:
        raise AssertionError(f"deleted tree {tid} reads back {after}, not 404/410")
    ctx.fixtures.mark_deleted("genealogies_tree", tid)
    if sibling not in l1.items:
        raise AssertionError(f"the other tree {sibling} is no longer listed; "
                             f"{describe_box(l1.result)}")
    expect_same(s0, read_tree(ctx, sibling), f"the other tree {sibling}")


WRITES_GENEALOGIES_TREES = (
    Recipe("writes.genealogies.tree_create", ("createGenealogiesTree",), "state-change",
           tree_create),
    Recipe("writes.genealogies.tree_update", ("updateGenealogiesTree",), "state-change",
           tree_update),
    Recipe("writes.genealogies.tree_delete", ("deleteGenealogiesTree",), "state-change",
           tree_delete),
)
