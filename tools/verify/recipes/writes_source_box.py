"""Source box writes: user-defined collections (source folders) and their members.

One recipe per write, each claiming only the write it calls through ``ctx.api``.
Every collection and description written is a ZZSPIKE fixture this run created and
tracked: ``FixtureSet.collection`` (the raw client, with the body the claimed create
sends too) and ``FixtureSet.source`` (the proven library create); membership another
recipe needs is planted with ``FixtureSet.add_to_collection``. Teardown deletes the
descriptions, then the (now empty) collections.

The read-backs are unclaimed and, unlike the P3 ones, not yet verified (nothing else
reads the source box): readUserDefinedCollection (a folder's title),
readUserDefinedCollectionSourceDescriptions (its members; 204 when empty) and
readSourceFolders (the signed-in user's folders, by beta's redirect to
``/sources/{uid}/collections``). Each negative control compares two answers of the
same read, so a read that ignored the write would fail it; membership is asserted
exactly (the intended descriptions and no other). The removal names the user as
``current`` (the spec's example; beta answered the same path's GET for it), so no
account id is sent; failure text drops links and attribution and redacts account ids
(ruling I-4).
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from ..fixtures import COLLECTION_GONE, collection_body, descriptions_body
from .writes_common import (GONE, Listing, created_id, describe_box, expect_same, folders,
                            members, read_description, read_folder, redacted, status_of,
                            token)
from .writes_common import poll as _poll

USER = "current"  # the uid path segment of the removal (spec example)


def poll(ctx: VerifyContext, fetch, done, what: str):
    """``writes_common.poll``, failing with the source box's account-free skeleton."""
    return _poll(ctx, fetch, done, what, shown=describe_box)


def _own(ctx: VerifyContext, kind: str, oid: str) -> None:
    if oid not in ctx.fixtures.tracked(kind):
        raise AssertionError(f"refusing to write {kind} {oid}: not created and tracked by "
                             "this run")


def _no_new_folders(ctx: VerifyContext, before: Listing, after: Listing,
                    expected: set[str], ours: set[str]) -> None:
    """Fail the recipe for any folder that appeared besides ``expected``: the write made
    a collection nobody asked for. Only a stray titled with a marker this recipe itself
    posted (``ours``) is tracked, so teardown deletes it; a folder anyone else made in
    the window is never adopted (and never deleted)."""
    stray = sorted(set(after.items) - set(before.items) - expected)
    for udcid in stray:
        if after.items[udcid] in ours:
            ctx.fixtures.track("collection", udcid)
    if stray:
        raise AssertionError(f"new folder(s) {stray} appeared besides {sorted(expected)}; "
                             f"{describe_box(after.result)}")


def _exactly(found: Listing, wanted: set[str], what: str) -> None:
    if set(found.items) != wanted:
        raise AssertionError(f"{what} holds {sorted(found.items)}, not exactly "
                             f"{sorted(wanted)}; {describe_box(found.result)}")


@redacted
def collection_create(ctx: VerifyContext) -> None:
    """Create a ZZSPIKE collection with createSourceFolder; read it back.

    Body ``{"collections": [{"title": marker}]}``; the id is the create's X-ENTITY-ID
    or Entity-Id (the spec: "Expect an Entity-Id header"), else its Location, tracked at
    once; with none, the one new folder bearing the marker (every such folder tracked).
    Negative control: the new id among the user's folders (readSourceFolders), before
    (absent) and after (polled). The folder list must name it with the marker title
    and show no other new folder (a stray is tracked for teardown only if it bears our
    marker; a foreign one fails the recipe but is left alone), and readUserDefinedCollection must answer the title.
    """
    title = token("f")
    before = folders(ctx)
    if title in before.items.values():
        raise AssertionError(f"a folder is titled {title} before the create")
    result = ctx.api.create_source_folder(reason=ctx.reason, body=collection_body(title))
    udcid = created_id(result, "collection")
    if udcid:
        ctx.fixtures.track("collection", udcid)
    lowered = {k.lower(): v for k, v in result.headers.items()}
    ctx.notes.append(f"collection create answered {result.status} "
                     f"Location={lowered.get('location')!r} "
                     f"X-ENTITY-ID={lowered.get('x-entity-id')!r} "
                     f"Entity-Id={lowered.get('entity-id')!r}")
    if not udcid:
        udcid = _the_new_folder(ctx, before, title)
    after = poll(ctx, lambda: folders(ctx), lambda f: udcid in f.items,
                 f"readSourceFolders: collection {udcid}")
    _no_new_folders(ctx, before, after, {udcid}, {title})
    ctx.expect_changed(udcid in before.items, udcid in after.items,
                       f"presence of collection {udcid} among the user's folders")
    if after.items[udcid] != title:
        raise AssertionError(f"collection {udcid} is listed titled {after.items[udcid]!r}, "
                             f"not {title!r}; {describe_box(after.result)}")
    folder = read_folder(ctx, udcid)
    if folder.title != title:
        raise AssertionError(f"collection {udcid} reads back titled {folder.title!r}, not "
                             f"{title!r}; {describe_box(folder.result)}")


def _the_new_folder(ctx: VerifyContext, before: Listing, title: str) -> str:
    """No id answered: every new folder titled ``title`` is ours (tracked at once); the
    create proves something only if there is exactly one."""
    after = poll(ctx, lambda: folders(ctx),
                 lambda f: any(t == title for u, t in f.items.items() if u not in before.items),
                 f"readSourceFolders: a new folder titled {title}")
    new = [u for u, t in after.items.items() if u not in before.items and t == title]
    for found in new:
        ctx.fixtures.track("collection", found)
    if len(new) != 1:
        raise AssertionError(f"the create answered no id and {len(new)} new folders are "
                             f"titled {title}; {describe_box(after.result)}")
    ctx.notes.append(f"collection create answered no id: {new[0]} is the one new folder "
                     f"titled {title}")
    return new[0]


@redacted
def collection_update(ctx: VerifyContext) -> None:
    """Rename a fixture collection with updateUserDefinedCollection.

    Two ZZSPIKE collections; only the first is posted, as
    ``{"collections": [{"id": udcid, "title": new}]}``. Negative control: its title read
    back with readUserDefinedCollection, before and after; it must be the new one. The
    folder list must show no new folder (an update that made one fails; the stray is
    tracked only if it bears our new title) and the new title under the same id. Sibling: the other collection reads
    back as it was.
    """
    udcid = ctx.fixtures.collection(token("f"))
    sibling = ctx.fixtures.collection(token("g"))
    _own(ctx, "collection", udcid)
    f0, s0, l0 = read_folder(ctx, udcid), read_folder(ctx, sibling), folders(ctx)
    title = token("r")
    ctx.api.update_user_defined_collection(udcid, reason=ctx.reason,
                                           body=collection_body(title, udcid=udcid))
    l1 = folders(ctx)
    _no_new_folders(ctx, l0, l1, set(), {title})
    f1 = read_folder(ctx, udcid)
    ctx.expect_changed(f0.title, f1.title, f"title of collection {udcid}")
    if f1.title != title or l1.items.get(udcid) != title:
        raise AssertionError(f"collection {udcid} reads back {f1.title!r} and is listed "
                             f"{l1.items.get(udcid)!r} after the rename to {title!r}; "
                             f"{describe_box(l1.result)}")
    expect_same(s0, read_folder(ctx, sibling), f"the other collection {sibling}")


@redacted
def collection_add(ctx: VerifyContext) -> None:
    """Add two fixture descriptions to a fixture collection with
    updateSourceDescriptionsToCollection.

    A third description is first planted in the collection with
    ``FixtureSet.add_to_collection`` (not claimed), then one POST of
    ``{"sourceDescriptions": [{"id": a}, {"id": b}]}``; a fourth is never added.
    Negative control: the collection's members
    (readUserDefinedCollectionSourceDescriptions), before (exactly the planted one,
    polled) and after (polled until both show). Membership is then exactly the planted
    one plus the two (so an add that replaced the members fails), each under its own title.
    """
    udcid = ctx.fixtures.collection(token("f"))
    planted = ctx.fixtures.source(token("p"))
    titles = {ctx.fixtures.source(t): t for t in (token("a"), token("b"))}
    ctx.fixtures.source(token("o"))  # never added: must not show up
    _own(ctx, "collection", udcid)
    ctx.fixtures.add_to_collection(udcid, [planted])
    m0 = poll(ctx, lambda: members(ctx, udcid), lambda m: planted in m.items,
              f"members of collection {udcid}: {planted} planted")
    _exactly(m0, {planted}, f"collection {udcid} before the add")
    ctx.api.update_source_descriptions_to_collection(udcid, reason=ctx.reason,
                                                     body=descriptions_body(list(titles)))
    m1 = poll(ctx, lambda: members(ctx, udcid), lambda m: set(titles) <= set(m.items),
              f"members of collection {udcid}: {sorted(titles)}")
    ctx.expect_changed(sorted(m0.items), sorted(m1.items), f"members of collection {udcid}")
    _exactly(m1, {planted, *titles}, f"collection {udcid} (planted plus added)")
    expect_same({planted: m0.items[planted], **titles}, dict(m1.items),
                f"the titles of the members of {udcid}")


@redacted
def collection_remove(ctx: VerifyContext) -> None:
    """Remove one of two members of a fixture collection with
    deleteSourceDescriptionsFromCollections.

    Both are moved in with ``FixtureSet.add_to_collection`` (not claimed); the removal
    is ``DELETE /sources/current/collections/descriptions?id={a}``. Negative control: a
    among the collection's members, before (present, polled) and after (absent, polled).
    Membership is then exactly b. The op removes a from collections: a must still read
    back, unchanged (if it is gone, the recipe fails and teardown skips it).
    """
    udcid = ctx.fixtures.collection(token("f"))
    a, b = ctx.fixtures.source(token("a")), ctx.fixtures.source(token("b"))
    ctx.fixtures.add_to_collection(udcid, [a, b])
    m0 = poll(ctx, lambda: members(ctx, udcid), lambda m: {a, b} <= set(m.items),
              f"members of collection {udcid}: {a} and {b} planted")
    _exactly(m0, {a, b}, f"collection {udcid} before the removal")
    _own(ctx, "source", a)
    a0 = read_description(ctx, a)
    ctx.api.delete_source_descriptions_from_collections(USER, reason=ctx.reason,
                                                        query={"id": a})
    m1 = poll(ctx, lambda: members(ctx, udcid), lambda m: a not in m.items,
              f"members of collection {udcid}: {a} removed")
    ctx.expect_changed(a in m0.items, a in m1.items, f"membership of {a} in {udcid}")
    _exactly(m1, {b}, f"collection {udcid} after removing {a}")
    gone = status_of(lambda: ctx.api.read_source_description(a))
    if gone in GONE:
        ctx.fixtures.mark_deleted("source", a)
        raise AssertionError(f"removing {a} from the collections deleted the description "
                             f"itself (it reads back {gone})")
    expect_same(a0, read_description(ctx, a), f"the removed description {a}")


@redacted
def collection_delete(ctx: VerifyContext) -> None:
    """Delete a fixture collection with deleteUserDefinedCollection; read it back gone.

    Two empty ZZSPIKE collections; only the first is deleted. Negative control: its id
    among the user's folders (readSourceFolders), before (present) and after (absent,
    polled); readUserDefinedCollection must then error 404/410, or 400 (beta's answer
    for an unknown collection id) with the folder list agreeing. Only then is it marked
    deleted. Sibling: the other collection is still listed and reads back as it was.
    """
    udcid = ctx.fixtures.collection(token("f"))
    sibling = ctx.fixtures.collection(token("g"))
    _own(ctx, "collection", udcid)
    read_folder(ctx, udcid)
    s0, l0 = read_folder(ctx, sibling), folders(ctx)
    ctx.api.delete_user_defined_collection(udcid, reason=ctx.reason)
    l1 = poll(ctx, lambda: folders(ctx), lambda f: udcid not in f.items,
              f"readSourceFolders: collection {udcid} gone")
    ctx.expect_changed(udcid in l0.items, udcid in l1.items,
                       f"presence of collection {udcid} among the user's folders")
    after = status_of(lambda: ctx.api.read_user_defined_collection(udcid))
    if after not in COLLECTION_GONE:
        raise AssertionError(f"deleted collection {udcid} reads back {after}, not one of "
                             f"{list(COLLECTION_GONE)}")
    ctx.fixtures.mark_deleted("collection", udcid)
    if l1.items.get(sibling) != s0.title:
        raise AssertionError(f"the other collection {sibling} is not listed as "
                             f"{s0.title!r}; {describe_box(l1.result)}")
    expect_same(s0, read_folder(ctx, sibling), f"the other collection {sibling}")


WRITES_SOURCE_BOX = (
    Recipe("writes.sources.collection_create", ("createSourceFolder",), "state-change",
           collection_create),
    Recipe("writes.sources.collection_update", ("updateUserDefinedCollection",),
           "state-change", collection_update),
    Recipe("writes.sources.collection_add", ("updateSourceDescriptionsToCollection",),
           "state-change", collection_add),
    Recipe("writes.sources.collection_remove", ("deleteSourceDescriptionsFromCollections",),
           "state-change", collection_remove),
    Recipe("writes.sources.collection_delete", ("deleteUserDefinedCollection",),
           "state-change", collection_delete),
)
