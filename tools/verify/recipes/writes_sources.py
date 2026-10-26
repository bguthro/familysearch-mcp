"""Source description writes: create, update and delete, one recipe per write.

A source description is shared data, so only ZZSPIKE descriptions this run created
and tracked are ever written: the create's own (tracked the moment its id is known),
or ``FixtureSet.source`` ones (the proven library create, ``Sources.create_description``,
whose body the create sends too). Each is read back with the verified
readSourceDescription (not claimed): its ``about``, title and citation. Teardown
deletes every tracked description except one a recipe deleted and saw gone.

The update posts the whole description (id, ``about``, title, citation): the spec
lists those as the supported properties. Should beta answer it with the id of another
description (an update that made a new one), that one is tracked too and the recipe
fails.
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from .writes_common import (GONE, created_id, description_body, describe_box, expect_same,
                            read_description, status_of, token)


def _about(title: str) -> str:
    return f"https://example.org/{title.lower()}"


def _box_titles(ctx: VerifyContext) -> list[str]:
    """Every title in the signed-in user's source box (readUserSourceDescriptions with
    uid ``current``, unclaimed: beta answers the box whatever the uid). 204: none."""
    result = ctx.api.read_user_source_descriptions("current")
    if result.status == 204 or not (result.text or "").strip():
        return []
    body = result.body if isinstance(result.body, dict) else {}
    return [t.get("value") or "" for d in body.get("sourceDescriptions") or []
            if isinstance(d, dict) for t in d.get("titles") or [] if isinstance(t, dict)]


def _own(ctx: VerifyContext, sid: str) -> None:
    if sid not in ctx.fixtures.tracked("source"):
        raise AssertionError(f"refusing to write source description {sid}: not created "
                             "and tracked by this run")


def description_create(ctx: VerifyContext) -> None:
    """Create a ZZSPIKE source description with createSourceDescription; read it back.

    The body is the library's proven one (about, title, citation); the id is the
    create's X-ENTITY-ID, else its Location, and is tracked at once. Negative control
    (as the pilot's person create): "does a description titled with the fresh marker
    exist?" Before the create, the user's source box answers it (no title carries the
    marker); after, the verified readSourceDescription of the new id answers the marker
    title. It must also answer the posted about and citation.
    """
    title = token("t")
    before = any(title in t for t in _box_titles(ctx))
    citation, about = f"{title} citation", _about(title)
    result = ctx.api.create_source_description(
        reason=ctx.reason, body=description_body(about, title, citation))
    sid = created_id(result, "source description")
    ctx.fixtures.track("source", sid)
    d = read_description(ctx, sid)
    ctx.expect_changed(before, title in (d.title or ""),
                       f"presence of a source description titled {title}")
    if (d.title, d.citation, d.about) != (title, citation, about):
        raise AssertionError(f"source description {sid} reads back {d!r}, not the posted "
                             f"title, citation and about; {describe_box(d.result)}")


def description_update(ctx: VerifyContext) -> None:
    """Change the title and citation of a fixture description with updateSourceDescription.

    Two ZZSPIKE descriptions are made with the library; only the first is updated, with
    the whole description (its id and about, a fresh title and citation).
    Negative control: its title, read back with readSourceDescription, before and
    after. It must read back the new title and citation under the same id (so the old
    title is gone) and the about unchanged. Sibling: the other description reads back as it was.
    """
    sid = ctx.fixtures.source(token("u"))
    sibling = ctx.fixtures.source(token("k"))
    _own(ctx, sid)
    d0, s0 = read_description(ctx, sid), read_description(ctx, sibling)
    title, citation = token("t"), token("c")
    result = ctx.api.update_source_description(
        sid, reason=ctx.reason, body=description_body(d0.about, title, citation, sid=sid))
    other = result.entity_id
    if other and other != sid:
        ctx.fixtures.track("source", other)
        raise AssertionError(f"the update of {sid} answered another description's id "
                             f"{other} (status {result.status}): it made a new one")
    d1 = read_description(ctx, sid)
    ctx.expect_changed(d0.title, d1.title, f"title of source description {sid}")
    if (d1.title, d1.citation) != (title, citation):
        raise AssertionError(f"source description {sid} reads back title {d1.title!r} and "
                             f"citation {d1.citation!r} after the update to {title!r} / "
                             f"{citation!r}; {describe_box(d1.result)}")
    expect_same(d0.about, d1.about, f"the about of source description {sid}")
    expect_same(s0, read_description(ctx, sibling), f"the other source description {sibling}")


def description_delete(ctx: VerifyContext) -> None:
    """Delete a fixture description with deleteSourceDescription; read it back gone.

    Two ZZSPIKE descriptions are made with the library; only the first is deleted.
    Negative control: readSourceDescription's status for it, before (readable) and after
    (404/410); only then is it marked deleted, so teardown skips it. Sibling: the
    other description reads back as it was.
    """
    sid = ctx.fixtures.source(token("d"))
    sibling = ctx.fixtures.source(token("k"))
    _own(ctx, sid)
    before = status_of(lambda: ctx.api.read_source_description(sid))
    s0 = read_description(ctx, sibling)
    ctx.api.delete_source_description(sid, reason=ctx.reason)
    after = status_of(lambda: ctx.api.read_source_description(sid))
    ctx.expect_changed(before, after, f"readability of source description {sid}")
    if after not in GONE:
        raise AssertionError(f"deleted source description {sid} reads back {after}, "
                             "not 404/410")
    ctx.fixtures.mark_deleted("source", sid)
    expect_same(s0, read_description(ctx, sibling), f"the other source description {sibling}")


WRITES_SOURCES = (
    Recipe("writes.sources.description_create", ("createSourceDescription",),
           "state-change", description_create),
    Recipe("writes.sources.description_update", ("updateSourceDescription",),
           "state-change", description_update),
    Recipe("writes.sources.description_delete", ("deleteSourceDescription",),
           "state-change", description_delete),
)
