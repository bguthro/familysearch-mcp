"""User Tree writes (Task C): create, update and delete a group and a user tree.

One recipe per write, each claiming only the write it calls through ``ctx.api``. A group
or tree is a ZZSPIKE fixture this run created and tracked (the create's own id, or
``FixtureSet.user_object``), deleted by strict teardown. Read-backs are by id
(readGroup, readTree: verified by the ``reads.usertree.*`` cases), never the lists. The
account's current tree is not touched: nothing here posts to ``/trees/current``.

The bodies are guesses (``{"groups": [{"name"}]}`` / ``{"trees": [{"name", ...}]}``); a
tree is created private and hidden. The create's status and headers go into ``ctx.notes``.
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from ..fixtures import user_object_body
from .user_tree_common import NO_SUCH_USER_OBJECT, read_group, read_user_tree
from .writes_common import (GONE, created_id, describe_box, expect_same, redacted,
                            status_of, token)

KINDS = {
    "group": dict(envelope="groups", read=read_group, label="group", tag="g"),
    "user_tree": dict(envelope="trees", read=read_user_tree, label="user tree", tag="u"),
}


def _api(ctx: VerifyContext, kind: str, verb: str):
    return getattr(ctx.api, f"{verb}_{'group' if kind == 'group' else 'tree'}")


def _status(ctx: VerifyContext, kind: str, oid: str) -> int:
    return status_of(lambda: _api(ctx, kind, "read")(oid))


def _own(ctx: VerifyContext, kind: str, oid: str) -> None:
    if oid not in ctx.fixtures.tracked(kind):
        raise AssertionError(f"refusing to write {kind} {oid}: not created and tracked by "
                             "this run")


def _create(kind: str):
    spec = KINDS[kind]

    @redacted
    def run(ctx: VerifyContext) -> None:
        name = token(spec["tag"])
        unknown = _status(ctx, kind, NO_SUCH_USER_OBJECT)
        extra = {"private": True, "hidden": True} if kind == "user_tree" else {}
        result = _api(ctx, kind, "create")(
            reason=ctx.reason, body=user_object_body(kind, name, description="ZZSPIKE fixture",
                                                      **extra))
        lowered = {k.lower(): v for k, v in result.headers.items()}
        ctx.notes.append(f"{spec['label']} create answered {result.status} "
                         f"Location={lowered.get('location')!r} "
                         f"X-ENTITY-ID={lowered.get('x-entity-id')!r}")
        oid = created_id(result, spec["label"])
        if not oid:
            raise AssertionError(f"the create answered no X-ENTITY-ID or Location; "
                                 f"{describe_box(result)}")
        ctx.fixtures.track(kind, oid)
        back = spec["read"](ctx, oid)
        ctx.expect_changed(unknown, back.result.status,
                           f"readability of {spec['label']} {oid} (vs an unknown id)")
        if back.name != name:
            raise AssertionError(f"{spec['label']} {oid} reads back named {back.name!r}, not "
                                 f"{name!r}; {describe_box(back.result)}")
    run.__doc__ = f"""Create a ZZSPIKE {spec['label']} with create{'Group' if kind == 'group' else 'Tree'}; read it by id.

    The id is the create's X-ENTITY-ID / Location, tracked at once. Negative control:
    the read's status for an id that does not exist (an error) against the new id's
    (200). The read must name the id with the marker name.
    """
    return run


def _update(kind: str):
    spec = KINDS[kind]

    @redacted
    def run(ctx: VerifyContext) -> None:
        oid = ctx.fixtures.user_object(kind, token(spec["tag"]))
        sibling = ctx.fixtures.user_object(kind, token("s"))
        _own(ctx, kind, oid)
        b0, s0 = spec["read"](ctx, oid), spec["read"](ctx, sibling)
        name = token("r")
        result = _api(ctx, kind, "update")(
            oid, reason=ctx.reason, body=user_object_body(kind, name, oid=oid))
        other = created_id(result, spec["label"])
        if other and other != oid:
            ctx.fixtures.track(kind, other)
            raise AssertionError(f"the update of {oid} answered another id {other}: it made "
                                 "a new one")
        b1 = spec["read"](ctx, oid)
        ctx.expect_changed(b0.name, b1.name, f"name of {spec['label']} {oid}")
        if b1.name != name:
            raise AssertionError(f"{spec['label']} {oid} reads back named {b1.name!r}, not "
                                 f"{name!r}; {describe_box(b1.result)}")
        expect_same(b0.other_than_name(), b1.other_than_name(),
                    f"the other fields of {spec['label']} {oid}")
        expect_same(s0, spec["read"](ctx, sibling), f"the other {spec['label']} {sibling}")
    run.__doc__ = f"""Rename a fixture {spec['label']} with update{'Group' if kind == 'group' else 'Tree'}.

    Two ZZSPIKE {spec['label']}s; only the first is posted, ``name`` with its id.
    Negative control: its name read by id before and after (the new marker). Siblings: the
    entry's other fields (description, access) are unchanged, no new id is answered, and the
    other {spec['label']} reads back as it was.
    """
    return run


def _delete(kind: str):
    spec = KINDS[kind]

    @redacted
    def run(ctx: VerifyContext) -> None:
        oid = ctx.fixtures.user_object(kind, token(spec["tag"]))
        sibling = ctx.fixtures.user_object(kind, token("s"))
        _own(ctx, kind, oid)
        before = spec["read"](ctx, oid).result.status
        s0 = spec["read"](ctx, sibling)
        _api(ctx, kind, "delete")(oid, reason=ctx.reason)
        after = _status(ctx, kind, oid)
        ctx.expect_changed(before, after, f"readability of {spec['label']} {oid}")
        if after not in GONE:
            raise AssertionError(f"deleted {spec['label']} {oid} reads back {after}, not "
                                 "404/410")
        ctx.fixtures.mark_deleted(kind, oid)
        expect_same(s0, spec["read"](ctx, sibling), f"the other {spec['label']} {sibling}")
    run.__doc__ = f"""Delete a fixture {spec['label']} with delete{'Group' if kind == 'group' else 'Tree'}; read it gone.

    Two ZZSPIKE {spec['label']}s; only the first is deleted. Negative control: the read's
    status for it, before (200) and after (404/410); only then is it marked deleted. Sibling:
    the other {spec['label']} reads back as it was.
    """
    return run


WRITES_USER_TREES = (
    Recipe("writes.usertree.group_create", ("createGroup",), "state-change", _create("group")),
    Recipe("writes.usertree.group_update", ("updateGroup",), "state-change", _update("group")),
    Recipe("writes.usertree.group_delete", ("deleteGroup",), "state-change", _delete("group")),
    Recipe("writes.usertree.tree_create", ("createTree",), "state-change",
           _create("user_tree")),
    Recipe("writes.usertree.tree_update", ("updateTree",), "state-change",
           _update("user_tree")),
    Recipe("writes.usertree.tree_delete", ("deleteTree",), "state-change",
           _delete("user_tree")),
)

# setCurrentTree is account state (the current tree is GLOBAL and must never change). A
# capture-and-restore recipe could set a ZZSPIKE tree and restore GLOBAL in a finally, but
# a failed restore would leave the account on a tree that teardown then deletes; left for
# Ben to rule on, never run.
DEFERRED = {
    "setCurrentTree": (
        "changes the signed-in account's current tree (GLOBAL), which no recipe may do unless "
        "the restore is fail-safe: a capture-and-restore through a ZZSPIKE user tree whose "
        "restore failing would leave the account pointing at a tree teardown deletes; not "
        "attempted pending Ben's ruling"),
}
