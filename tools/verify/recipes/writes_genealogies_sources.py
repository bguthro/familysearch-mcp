"""Private (Genealogies) source description writes: create, update and delete in a tree.

Each recipe claims one write. Every description is a ZZSPIKE fixture in a tree this run
created and tracked: the create's own (tracked the moment its id is known) or
``FixtureSet.genealogies_source`` ones; teardown deletes them before their tree. They
are read back with readGenealogiesSourceDescription (unclaimed here, verified by
``reads.genealogies.source``). The body is the Family Tree description's (about, title,
citation), unproven here. There is no read that lists a tree's sources, so the create's
negative control compares the unknown-id read (an error) with the created id's (200).
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from ..fixtures import same_id
from .genealogies_common import NO_SUCH_GENEALOGIES
from .writes_common import poll as _poll
from .writes_common import (GONE, created_id, describe_box, description_body, description_view,
                            expect_same, redacted, require_created_id, status_of, token)


def poll(ctx, fetch, done, what, *, shown=None):
    return _poll(ctx, fetch, done, what, shown=shown or describe_box)


def _read(ctx: VerifyContext, sdid: str):
    # Live: the source 2:2:286671 is answered as s_286671 (same_id maps one to the other).
    return description_view(ctx.api.read_genealogies_source_description(sdid), sdid,
                            same=same_id)


def _own(ctx: VerifyContext, sdid: str) -> None:
    if sdid not in ctx.fixtures.tracked("genealogies_source"):
        raise AssertionError(f"refusing to write source description {sdid}: not created and "
                             "tracked by this run")


@redacted
def source_create(ctx: VerifyContext) -> None:
    """Create a ZZSPIKE source description in a fixture tree with
    createGenealogiesSourceDescription; read it back.

    The id is the create's X-ENTITY-ID / Location, tracked at once. Negative control: the
    status of readGenealogiesSourceDescription for an id that does not exist (an error)
    against the new id's (200). It must answer the posted about, title and citation, and
    a second read of the unknown id must still error.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    title = token("t")
    about, citation = f"https://example.org/{title.lower()}", f"{title} citation"
    before = status_of(lambda: ctx.api.read_genealogies_source_description(NO_SUCH_GENEALOGIES))
    result = ctx.api.create_genealogies_source_description(
        tid, reason=ctx.reason, body=description_body(about, title, citation))
    ctx.notes.append(f"source create answered {result.status}")
    sdid = require_created_id(result, "private source description")
    ctx.fixtures.track("genealogies_source", sdid)
    d = _read(ctx, sdid)
    ctx.expect_changed(before, d.result.status,
                       f"readability of source description {sdid} (vs an unknown id)")
    if (d.title, d.citation, d.about) != (title, citation, about):
        raise AssertionError(f"source description {sdid} reads back {d!r}, not the posted "
                             f"title, citation and about; {describe_box(d.result)}")


@redacted
def source_update(ctx: VerifyContext) -> None:
    """Change the title and citation of a fixture description with
    updateGenealogiesSourceDescription.

    Two ZZSPIKE descriptions in one fixture tree; only the first is updated, with the
    whole description (its id and about, a fresh title and citation). Negative control:
    its title, read back, before and after. Its titles and citations must each be exactly
    the new value under the same id with the about unchanged, and the update must not
    answer another description's id. Sibling: the other description reads back as it was.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    sdid = ctx.fixtures.genealogies_source(tid, token("u"))
    sibling = ctx.fixtures.genealogies_source(tid, token("k"))
    _own(ctx, sdid)
    d0, s0 = _read(ctx, sdid), _read(ctx, sibling)
    title, citation = token("t"), token("c")
    result = ctx.api.update_genealogies_source_description(
        sdid, reason=ctx.reason, body=description_body(d0.about, title, citation, sid=sdid))
    other = created_id(result, "private source description")
    if other and other != sdid:
        ctx.fixtures.track("genealogies_source", other)
        raise AssertionError(f"the update of {sdid} answered another description's id {other} "
                             f"(status {result.status}): it made a new one")
    d1 = _read(ctx, sdid)
    ctx.expect_changed(d0.title, d1.title, f"title of source description {sdid}")
    if d1.titles != (title,) or d1.citations != (citation,):
        raise AssertionError(f"source description {sdid} reads back titles {list(d1.titles)!r} "
                             f"and citations {list(d1.citations)!r} after the update to "
                             f"{title!r} / {citation!r}; {describe_box(d1.result)}")
    expect_same(d0.about, d1.about, f"the about of source description {sdid}")
    expect_same(s0, _read(ctx, sibling), f"the other source description {sibling}")


@redacted
def source_delete(ctx: VerifyContext) -> None:
    """Delete a fixture description with deleteGenealogiesSourceDescription; read it gone.

    Two ZZSPIKE descriptions in one fixture tree; only the first is deleted. Negative control:
    readGenealogiesSourceDescription's status for it, before (200) and after (404/410); only then is it marked deleted, so teardown skips it. Sibling: the other
    description reads back as it was.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    sdid = ctx.fixtures.genealogies_source(tid, token("d"))
    sibling = ctx.fixtures.genealogies_source(tid, token("k"))
    _own(ctx, sdid)
    before = status_of(lambda: ctx.api.read_genealogies_source_description(sdid))
    s0 = _read(ctx, sibling)
    ctx.api.delete_genealogies_source_description(sdid, reason=ctx.reason)
    after = poll(ctx, lambda: status_of(lambda: ctx.api.read_genealogies_source_description(
        sdid)), lambda s: s in GONE, f"readGenealogiesSourceDescription: {sdid} gone")
    ctx.expect_changed(before, after, f"readability of source description {sdid}")
    if after not in GONE:
        raise AssertionError(f"deleted source description {sdid} reads back {after}, "
                             "not 404/410")
    ctx.fixtures.mark_deleted("genealogies_source", sdid)
    expect_same(s0, _read(ctx, sibling), f"the other source description {sibling}")


WRITES_GENEALOGIES_SOURCES = (
    Recipe("writes.genealogies.source_create", ("createGenealogiesSourceDescription",),
           "state-change", source_create),
    Recipe("writes.genealogies.source_update", ("updateGenealogiesSourceDescription",),
           "state-change", source_update),
    Recipe("writes.genealogies.source_delete", ("deleteGenealogiesSourceDescription",),
           "state-change", source_delete),
)
