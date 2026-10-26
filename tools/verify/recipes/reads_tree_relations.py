"""Read recipes for the rest of the Family Tree reads: relationships, pedigrees, notes,
sources, memories.

Every case reads the fixture family (``dad`` + ``mom`` -> ``child``, their ``couple`` and
``child_and_parents`` relationships, and the notes, source and memory hung on them, built
by tools/verify/world.py) and anchors on a value the request does not send: a related
person's id or name, a note's text, a source's title, a memory's id. A relationship read
sends only the relationship id, so it anchors on the persons it joins; a single-note read
sends the note id, so it anchors on the text.

The source box reads anchor on the fixture collection (``collection``: a ZZSPIKE folder
holding the fixture source): its title, and the source's title among its members; the
request sends only the collection id.

Recipes are split by fixture group and by how sure the shape is, because one failing case
keeps its whole recipe from being recorded: the couple and child-and-parents notes
(envelopes not yet proven on beta) each have their own recipe, apart from the person
note, the source and the memory; the change feeds (whose entry content the recon never
showed) are apart from the relationship reads; the source queries (parameters the recon
never exercised) are apart from the source reads.

Shapes and statuses follow the recon (docs/superpowers/recon/2026-10-25-api-surface):
the dad has no notes, sources or memories (204, the empty variants below, each after its
populated case), relationship change histories are atom feeds linked to the
relationship, an unrelated note id answers 404. The reads that no obtainable fixture can
exercise under the read rules are in ``DEFERRED``.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from ..reads import ReadCase, json_path, reads_recipe
from .reads_tree_persons import (
    MISSING_PID, NO_SUCH_PERSON, _both, _expect_persons, _id, _name, _no_relatives, _pid,
)

# Well-formed, very unlikely to exist (tree and source ids share the person id format).
NO_SUCH_RELATIONSHIP = NO_SUCH_PERSON
NO_SUCH_SOURCE = NO_SUCH_PERSON
NO_SUCH_NOTE = "00000000-0000-4000-8000-000000000000"  # note ids are UUIDs (recon)
# Recon: an unknown collection id answered 400 "Unable to read collection" (and 400
# "Unable to load page of sources from source box" for its descriptions).
NO_SUCH_COLLECTION = NO_SUCH_PERSON
# Live (Task 4 run 1): right after the attach, the child's source-references answered 404,
# so the person -> source link may lag; the reads that follow it poll (an error is never
# retried, only an empty or anchor-less answer).
REFERENCE_EVENTUAL = (8, 5.0)
UNKNOWN_DESCRIPTION_EMPTY = (
    "beta answers readSourceReferences for an unknown description (ZZZZ-ZZZ) with an "
    "empty 204 (Task 4 live run 2), not the spec's 404; it counts only because the "
    "positive call in the same recipe named the child, which the request did not send")
SOURCE_HEAD_NO_VALIDATORS = (
    "beta answers readSourceDescriptionHead with no ETag, Last-Modified or Content-Length "
    "(Task 4 live run 2: Cache-Control, Content-Location, Date, Vary, Via, X-* only), and "
    "its GET (chunked, content-encoded) carries none either: existence semantics only")
NO_MATCHING_SDIDS_EMPTY = (
    "the spec documents 204 for readSourceDescriptions when 'no source descriptions "
    "matched provided ids', so an unknown sdid (ZZZZ-ZZZ) is an empty answer, not an "
    "error; it counts only because the positive call in the same recipe returned the "
    "fixture description under its title, which the request did not send")

DEFERRED: dict[str, str] = {
    "readDiscussion": (
        "needs a discussion fixture; createDiscussion is an unverified write, and recon "
        "got 404 for the spec's example id"),
    "readDiscussionHeaders": (
        "needs a discussion fixture (createDiscussion is an unverified write), and its "
        "sibling GET readDiscussion has none to answer content"),
    "readComments": (
        "needs a discussion with a comment; createDiscussion and createComment are "
        "unverified writes, and recon got 404 for the spec's example id"),
    "readCoupleRelationshipSources": (
        "recon answered 204 for the couple fixture; the world attaches its source only to "
        "the child, attaching one to a relationship is not in the hand-written library "
        "(an unverified write), and a 204 counts only after a populated case"),
    "readCoupleRelationshipSourceReferences": (
        "recon answered 204 for the couple fixture; a populated case needs a source "
        "attached to the couple, which no proven library call makes (unverified write)"),
    "getChildAndParentsRelationshipSources": (
        "recon answered 204 for the child-and-parents fixture; the world attaches its "
        "source only to the child, attaching one to a relationship is an unverified write"),
    "getChildAndParentsRelationshipSourceReferences": (
        "recon answered 204 for the child-and-parents fixture; a populated case needs a "
        "source attached to that relationship, which no proven library call makes"),
    "getSourceDescriptionChanges": (
        "ReadCase.body is static, not resolved against the world, so the POST body cannot "
        "name the source description created at run time; its control would be an "
        "unknown id, for which the spec documents a 204 ('none of the specified source "
        "descriptions has changed'), and an empty control is allowed only for a GET read; "
        "recon never called it"),
    "readSourceFolders": (
        "takes no path or query parameter, so no not-found control can be built (every "
        "request reads the same signed-in user's folders, by its documented 307); the "
        "source box write recipes use it, unclaimed, to list the folders"),
    "readUserSourceFolders": (
        "the uid path segment can only be 'current' (the sealed world holds no account "
        "id, ruling I-4), and beta ignored the uid of the sibling path "
        "/sources/{uid}/collections/descriptions (a bogus uid answered 200, P2 Task 4 "
        "live run 2); inferred from that sibling, not tried live: a not-found control on "
        "the uid is unlikely to hold"),
    "readUserSourceDescriptions": (
        "beta ignores or does not validate the uid path segment: with uid=current the "
        "positive call listed the fixture source, but the control uid=cis.user.ZZZZ-ZZZZ "
        "answered 200 too (Task 4 live run 2), so a not-found control is impossible; a "
        "control that must differ in content rather than error is not supported by the "
        "framework"),
    "getPreferredParentRelationship": (
        "documents only 204 (no preference) and 303 (redirect to the preferred "
        "relationship); a fresh fixture has no preference and setting one is an "
        "unverified PUT, and the world holds no user id for the uid path parameter"),
    "readPreferredSpouseRelationship": (
        "documents only 204 (no preference) and 303 (redirect); recon answered 204 for the "
        "child, setting a preference is an unverified PUT, and the world holds no user id"),
}


# ------------------------------------------------------------------ checks


def _tail(ref) -> str:
    """The id a reference names: its ``resourceId``, else the last segment of its URI or
    fragment (the query, such as ``?flag=fsh``, dropped)."""
    if isinstance(ref, dict):
        if isinstance(ref.get("resourceId"), str):
            return ref["resourceId"]
        ref = ref.get("resource")
    if not isinstance(ref, str):
        return ""
    return ref.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _record(body, key: str, rid: str) -> dict:
    """The record with id ``rid`` in the ``key`` list."""
    records = json_path(body, key)
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and record.get("id") == rid:
            return record
    raise AssertionError(f"{key} holds no record {rid}")


def _expect_couple(result, world):
    """The couple relationship, joining the dad and the mom."""
    rel = _record(result.body, "relationships", world.ids["couple"])
    got = {_tail(rel.get("person1")), _tail(rel.get("person2"))}
    want = {world.ids["dad"], world.ids["mom"]}
    assert got == want, f"the couple joins {sorted(got)}, not the dad and mom {sorted(want)}"


def _expect_child_and_parents(result, world):
    """The child-and-parents relationship: the dad and mom as parents, the child as child."""
    rel = _record(result.body, "childAndParentsRelationships", world.ids["child_and_parents"])
    parents = {_tail(rel.get("parent1")), _tail(rel.get("parent2"))}
    want = {world.ids["dad"], world.ids["mom"]}
    assert parents == want, f"the parents are {sorted(parents)}, not {sorted(want)}"
    assert _tail(rel.get("child")) == world.ids["child"], (
        f"the child is {_tail(rel.get('child'))!r}, not {world.ids['child']}")


def _expect_relationship_history(role: str, person: str):
    """A change feed linked to the relationship ``role``, with an entry naming ``person``."""

    def expect(result, world):
        rid = world.ids[role]
        href = json_path(result.body, "links.relationship.href")
        # Beta links with a query (``?flag=fsh``, as the person feeds): the path only.
        path = urlsplit(href).path.rstrip("/") if isinstance(href, str) else ""
        assert path.endswith("/" + rid), f"links.relationship.href {href!r} is not the {role}"
        entries = json_path(result.body, "entries")
        assert isinstance(entries, list) and entries, "no change entries"
        pid = world.ids[person]
        assert any(pid in json.dumps(e, ensure_ascii=False) for e in entries), (
            f"no change entry names the {person} {pid}")

    return expect


def _notes(body, envelope: str) -> list[dict]:
    owners = json_path(body, envelope)
    notes = []
    for owner in owners if isinstance(owners, list) else []:
        found = owner.get("notes") if isinstance(owner, dict) else None
        notes += [n for n in found if isinstance(n, dict)] if isinstance(found, list) else []
    return notes


def _expect_note(envelope: str, key: str, *, single: bool = False):
    """The ``envelope`` owner's notes hold the note ``key`` (by id) with its text.

    The world writes the text and a subject that starts with it, so either carries it.
    A single-note read must answer that note and no other.
    """

    def expect(result, world):
        nid, text = world.ids[key], world.names[f"{key}_text"]
        notes = _notes(result.body, envelope)
        by_id = {n.get("id"): n for n in notes}
        assert nid in by_id, f"{envelope} notes do not hold the {key} {nid}"
        note = by_id[nid]
        carried = [note.get(f) for f in ("text", "subject") if isinstance(note.get(f), str)]
        assert any(text in value for value in carried), (
            f"the {key} {nid} does not carry its text {text!r}")
        if single:
            assert set(by_id) == {nid}, f"answered notes {sorted(map(str, by_id))}, not one"

    return expect


def _titles(description: dict) -> list[str]:
    titles = description.get("titles") if isinstance(description, dict) else None
    return [t.get("value") if isinstance(t, dict) else t
            for t in titles if isinstance(t, (dict, str))] if isinstance(titles, list) else []


def _expect_description(key: str, title: str, *, exactly: bool = False):
    """``sourceDescriptions`` holds the ``key`` fixture (by id) titled ``title`` exactly
    (the citation also contains the title, so containment would not tell them apart)."""

    def expect(result, world):
        sid, want = world.ids[key], world.names[title]
        description = _record(result.body, "sourceDescriptions", sid)
        assert want in _titles(description), (
            f"the {key} {sid} is titled {_titles(description)}, not {want!r}")
        if exactly:
            ids = {d.get("id") for d in json_path(result.body, "sourceDescriptions")
                   if isinstance(d, dict)}
            assert ids == {sid}, f"descriptions are {sorted(map(str, ids))}, not the requested"

    return expect


def _expect_cited_by(role: str):
    """``persons`` holds ``role``, whose sources reference the fixture source description."""

    def expect(result, world):
        pid, sid = world.ids[role], world.ids["source_description"]
        person = _record(result.body, "persons", pid)
        refs = person.get("sources")
        tails = {_tail(r.get("description")) for r in refs if isinstance(r, dict)} if (
            isinstance(refs, list)) else set()
        assert sid in tails, f"the {role} {pid} does not reference the source {sid}"

    return expect


# ------------------------------------------------------------------ cases


def _arg(name: str, role: str):
    return lambda w: {name: w.ids[role]}


def _text(key: str):
    return lambda w: w.names[f"{key}_text"]


FAMILY = ("persons", "couple")
CRID, CAPRID = _arg("crid", "couple"), _arg("caprid", "child_and_parents")
MISSING_CRID = {"crid": NO_SUCH_RELATIONSHIP}
MISSING_CAPRID = {"caprid": NO_SUCH_RELATIONSHIP}

# The request sends only the relationship id: the persons it joins are the anchors.
RELATIONSHIPS_CASES = (
    ReadCase(op="readCoupleRelationship", path_args=CRID, missing=MISSING_CRID,
             expect=_expect_couple, anchors=(_id("dad"), _id("mom")), needs=FAMILY),
    ReadCase(op="readCoupleRelationshipHeaders", path_args=CRID,
             compare_with="readCoupleRelationship", missing=MISSING_CRID,
             expect=_expect_couple, anchors=(_id("dad"), _id("mom")), needs=FAMILY),
    ReadCase(op="getChildAndParentsRelationship", path_args=CAPRID, missing=MISSING_CAPRID,
             expect=_expect_child_and_parents,
             anchors=(_id("dad"), _id("mom"), _id("child")), needs=FAMILY),
    ReadCase(op="readChildAndParentsRelationshipHeaders", path_args=CAPRID,
             compare_with="getChildAndParentsRelationship", missing=MISSING_CAPRID,
             expect=_expect_child_and_parents,
             anchors=(_id("dad"), _id("mom"), _id("child")), needs=FAMILY),
)
RELATIONSHIPS = reads_recipe("reads.tree.relationships", RELATIONSHIPS_CASES)

# Atom feeds (the GET's Accept, which its HEAD is sent with too). The entries' content was
# never shown by the recon: a person the relationship joins is the anchor.
_COUPLE_HISTORY = _expect_relationship_history("couple", "dad")
_CAP_HISTORY = _expect_relationship_history("child_and_parents", "child")
RELATIONSHIP_HISTORY_CASES = (
    ReadCase(op="readCoupleRelationshipChangeHistory", path_args=CRID, missing=MISSING_CRID,
             expect=_COUPLE_HISTORY, anchors=(_id("dad"),), needs=FAMILY),
    ReadCase(op="readCoupleRelationshipChangeHistoryHeaders", path_args=CRID,
             compare_with="readCoupleRelationshipChangeHistory", missing=MISSING_CRID,
             expect=_COUPLE_HISTORY, anchors=(_id("dad"),), needs=FAMILY),
    ReadCase(op="readChildAndParentRelationship", path_args=CAPRID, missing=MISSING_CAPRID,
             expect=_CAP_HISTORY, anchors=(_id("child"),), needs=FAMILY),
    ReadCase(op="headChildAndParentRelationshipHistory", path_args=CAPRID,
             compare_with="readChildAndParentRelationship", missing=MISSING_CAPRID,
             expect=_CAP_HISTORY, anchors=(_id("child"),), needs=FAMILY),
)
RELATIONSHIP_HISTORY = reads_recipe("reads.tree.relationship_history",
                                    RELATIONSHIP_HISTORY_CASES)

# The query names one person: the relatives in the pedigree, by name, are the anchors.
PEDIGREE_CASES = (
    ReadCase(op="readAncestry", path_args=lambda w: {},
             query=lambda w: {"person": w.ids["child"]},
             missing={}, missing_query={"person": NO_SUCH_PERSON},
             expect=_expect_persons("child", "dad", "mom"),
             anchors=(_name("dad"), _name("mom")), needs=FAMILY),
    ReadCase(op="readDescendancy", path_args=lambda w: {},
             query=lambda w: {"person": w.ids["dad"], "generations": 2},
             missing={}, missing_query={"person": NO_SUCH_PERSON, "generations": 2},
             expect=_expect_persons("dad", "mom", "child"),
             anchors=(_name("child"), _name("mom")), needs=FAMILY),
)
PEDIGREE = reads_recipe("reads.tree.pedigree", PEDIGREE_CASES)

NOTES_CASES = (
    ReadCase(op="readPersonNotes", path_args=_pid("child"), missing=MISSING_PID,
             expect=_expect_note("persons", "person_note"),
             anchors=(_text("person_note"), _id("person_note")), needs=("person_note",)),
    ReadCase(op="readPersonNotes", path_args=_pid("dad"), missing=MISSING_PID,
             expect=_no_relatives("notes"), allow_empty=True, needs=("person_note",)),
    ReadCase(op="readPersonNote",
             path_args=lambda w: {"pid": w.ids["child"], "nid": w.ids["person_note"]},
             missing={"nid": NO_SUCH_NOTE},
             expect=_expect_note("persons", "person_note", single=True),
             anchors=(_text("person_note"),), needs=("person_note",)),
)
NOTES = reads_recipe("reads.tree.notes", NOTES_CASES)

COUPLE_NOTES_CASES = (
    ReadCase(op="getCoupleRelationshipNotes", path_args=CRID, missing=MISSING_CRID,
             expect=_expect_note("relationships", "couple_note"),
             anchors=(_text("couple_note"), _id("couple_note")), needs=("couple_note",)),
    ReadCase(op="readCoupleRelationshipNote",
             path_args=lambda w: {"crid": w.ids["couple"], "nid": w.ids["couple_note"]},
             missing={"nid": NO_SUCH_NOTE},
             expect=_expect_note("relationships", "couple_note", single=True),
             anchors=(_text("couple_note"),), needs=("couple_note",)),
)
COUPLE_NOTES = reads_recipe("reads.tree.couple_notes", COUPLE_NOTES_CASES)

_CAP_NOTE = "child_and_parents_note"
_CAP_ENVELOPE = "childAndParentsRelationships"
CHILD_AND_PARENTS_NOTES_CASES = (
    ReadCase(op="getChildAndParentsRelationshipNotes", path_args=CAPRID,
             missing=MISSING_CAPRID, expect=_expect_note(_CAP_ENVELOPE, _CAP_NOTE),
             anchors=(_text(_CAP_NOTE), _id(_CAP_NOTE)), needs=(_CAP_NOTE,)),
    ReadCase(op="readChildAndParentRelationshipNote",
             path_args=lambda w: {"caprid": w.ids["child_and_parents"], "nid": w.ids[_CAP_NOTE]},
             missing={"nid": NO_SUCH_NOTE},
             expect=_expect_note(_CAP_ENVELOPE, _CAP_NOTE, single=True),
             anchors=(_text(_CAP_NOTE),), needs=(_CAP_NOTE,)),
)
CHILD_AND_PARENTS_NOTES = reads_recipe("reads.tree.child_and_parents_notes",
                                       CHILD_AND_PARENTS_NOTES_CASES)


def _title():
    return lambda w: w.names["source_title"]


_SOURCE = _expect_description("source_description", "source_title")
SOURCES_CASES = (
    # The child's sources: its reference to the fixture description, and the description.
    ReadCase(op="readPersonSources", path_args=_pid("child"), missing=MISSING_PID,
             expect=_both(_SOURCE, _expect_cited_by("child")),
             anchors=(_title(), _id("source_description")), needs=("source",),
             eventual=REFERENCE_EVENTUAL),
    ReadCase(op="readPersonSources", path_args=_pid("dad"), missing=MISSING_PID,
             expect=_no_relatives("sources"), allow_empty=True, needs=("source",)),
    ReadCase(op="readSourceDescription", path_args=_arg("sdid", "source_description"),
             missing={"sdid": NO_SUCH_SOURCE}, expect=_SOURCE, anchors=(_title(),),
             needs=("source",)),
    ReadCase(op="readSourceDescriptionHead", path_args=_arg("sdid", "source_description"),
             compare_with="readSourceDescription", missing={"sdid": NO_SUCH_SOURCE},
             expect=_SOURCE, anchors=(_title(),), needs=("source",),
             validators_optional=SOURCE_HEAD_NO_VALIDATORS),
)
SOURCES = reads_recipe("reads.tree.sources", SOURCES_CASES)

SOURCE_QUERIES_CASES = (
    # The persons citing the fixture description: the child (the query sends only the
    # description id).
    ReadCase(op="readSourceReferences", path_args=lambda w: {},
             query=lambda w: {"description": w.ids["source_description"]},
             missing={}, missing_query={"description": NO_SUCH_SOURCE},
             expect=_expect_cited_by("child"), anchors=(_id("child"),), needs=("source",),
             eventual=REFERENCE_EVENTUAL, control_may_be_empty=UNKNOWN_DESCRIPTION_EMPTY),
    # The descriptions named by ``sdids``: exactly the fixture's, under its title.
    ReadCase(op="readSourceDescriptions", path_args=lambda w: {},
             query=lambda w: {"sdids": w.ids["source_description"]},
             missing={}, missing_query={"sdids": NO_SUCH_SOURCE},
             control_may_be_empty=NO_MATCHING_SDIDS_EMPTY,
             expect=_expect_description("source_description", "source_title", exactly=True),
             anchors=(_title(),), needs=("source",)),
)
SOURCE_QUERIES = reads_recipe("reads.tree.source_queries", SOURCE_QUERIES_CASES)

MEMORIES_CASES = (
    # The memory's description: its id is the memory id (the integration tests' read-back).
    ReadCase(op="getPersonMemories", path_args=_pid("child"), missing=MISSING_PID,
             expect=_expect_description("memory", "memory_title"),
             anchors=(_id("memory"), lambda w: w.names["memory_title"]), needs=("memory",)),
    ReadCase(op="getPersonMemories", path_args=_pid("dad"), missing=MISSING_PID,
             expect=_no_relatives("memories"), allow_empty=True, needs=("memory",)),
)
MEMORIES = reads_recipe("reads.tree.memories", MEMORIES_CASES)


def _expect_collection(result, world):
    """``collections`` holds the fixture collection (by id) under its own title."""
    udcid, want = world.ids["collection"], world.names["collection_title"]
    collection = _record(result.body, "collections", udcid)
    assert collection.get("title") == want, (
        f"collection {udcid} is titled {collection.get('title')!r}, not {want!r}")


_COLLECTION = lambda w: {"udcid": w.ids["collection"]}  # noqa: E731
# Membership is a collection's own list; the folder may show a moved source a little late.
MEMBERS_EVENTUAL = REFERENCE_EVENTUAL
# Each read is its own recipe: the source box shapes are not proven on beta yet.
COLLECTION_CASES = (
    ReadCase(op="readUserDefinedCollection", path_args=_COLLECTION,
             missing={"udcid": NO_SUCH_COLLECTION}, expect=_expect_collection,
             anchors=(lambda w: w.names["collection_title"],), needs=("collection",)),
)
COLLECTION = reads_recipe("reads.sources.collection", COLLECTION_CASES)
COLLECTION_DESCRIPTIONS_CASES = (
    # Its members: exactly the fixture source, under its title.
    ReadCase(op="readUserDefinedCollectionSourceDescriptions", path_args=_COLLECTION,
             missing={"udcid": NO_SUCH_COLLECTION},
             expect=_expect_description("source_description", "source_title", exactly=True),
             anchors=(_title(),), needs=("collection",), eventual=MEMBERS_EVENTUAL),
)
COLLECTION_DESCRIPTIONS = reads_recipe("reads.sources.collection_descriptions",
                                       COLLECTION_DESCRIPTIONS_CASES)

READS_TREE_RELATIONS = (RELATIONSHIPS, RELATIONSHIP_HISTORY, PEDIGREE, NOTES, COUPLE_NOTES,
                        CHILD_AND_PARENTS_NOTES, SOURCES, SOURCE_QUERIES, MEMORIES,
                        COLLECTION, COLLECTION_DESCRIPTIONS)
