"""Read recipes for the Memories reads, and the deferred Users and Utilities reads.

The memory cases read the ``memory`` fixture (a PNG uploaded to the child, titled with
its own ``ZZSPIKE`` value, and the persona the upload made for the child, both from
tools/verify/world.py) and anchor on what the request does not send: the memory's
title, or the child's id in the persona's tree identifier. The user's own memories are
apart, since whether beta honours the uid is unproven (it ignored it for the source
box). The Users and Utilities reads, and the memory comments, have no sound case under
the read rules: they are in ``DEFERRED``.
"""
from __future__ import annotations

import json

from ..reads import ReadCase, json_path, reads_recipe
from .reads_tree_relations import _titles

NO_SUCH_MEMORY = "999999999"  # memory ids are numeric (recon)
NO_SUCH_PERSONA = "999999999"
NO_SUCH_USER = "cis.user.ZZZZ-ZZZZ"
# Live (Task 4): a fresh upload may take a moment to show in a list.
MEMORY_EVENTUAL = (8, 5.0)
NO_MATCHING_MIDS_EMPTY = (
    "the spec documents 204 for getMemories 'upon a successful read with no results', so "
    "an unknown mid is an empty answer; it counts only because the positive call in the "
    "same recipe answered the fixture memory under its title, which the request did not "
    "send")
NO_USER_MEMORIES_EMPTY = (
    "the spec documents 204 for readUserMemories 'upon a successful read with no "
    "results' (and no 404), so an unknown user may be an empty answer; it counts only "
    "because the positive call answered the fixture memory under its title")

DEFERRED: dict[str, str] = {
    "readMemoryComments": (
        "a populated case needs a comment on the fixture memory, and createMemoryComments "
        "is an unverified write; recon's 200 for the fixture carried only the memory's "
        "description and an empty discussion, which proves nothing about comments"),
    "readCurrentUser": (
        "takes no path or query parameter, so no not-found control can be built: every "
        "request reads the same signed-in user (already verified by the hand-written "
        "library's read-back)"),
    "readPartnerEligibility": (
        "documents only 204 (and 403), so the framework accepts no 200 and a 204 counts "
        "only after a populated case; beta answers 403 for this account (recon sweep), "
        "a partner-only feature"),
    "readAgent": (
        "the only agent id obtainable is the signed-in account's own, which the world "
        "does not hold (no account ids in recipes), and its answer would put the account "
        "holder's name and email into failure skeletons (ruling I-4)"),
    "readUserHistory": (
        "recon answered an empty JSON object {} for the signed-in user's history, so no "
        "content can be anchored; filling it needs updateUserHistory, an unverified "
        "write, and the spec documents no not-found status for a control"),
    "readPendingModifications": (
        "takes no path or query parameter, so no not-found control can be built: every "
        "request reads the same feature list, and an unknown query parameter is ignored"),
}


# ------------------------------------------------------------------ checks


def _descriptions(body) -> list[dict]:
    found = json_path(body, "sourceDescriptions")
    return [d for d in found if isinstance(d, dict)] if isinstance(found, list) else []


def _expect_memory(*, exactly: bool = False):
    """A source description titled with the fixture memory's title; optionally the only one."""

    def expect(result, world):
        want = world.names["memory_title"]
        descriptions = _descriptions(result.body)
        assert any(want in _titles(d) for d in descriptions), (
            f"no description titled {want!r}")
        if exactly:
            titles = [_titles(d) for d in descriptions]
            assert len(descriptions) == 1, f"descriptions titled {titles}, not only the memory"

    return expect


def _expect_tree_persona(result, world):
    """A persona whose tree identifier is the child the memory was uploaded to."""
    child = world.ids["child"]
    persons = json_path(result.body, "persons")
    persons = persons if isinstance(persons, list) else []
    assert any(isinstance(p, dict) and child in json.dumps(p.get("identifiers"))
               for p in persons), f"no persona identifies the child {child}"


def _expect_persona(result, world):
    """The persona asked for, with the memory it belongs to."""
    pid = world.ids["memory_persona"]
    persons = json_path(result.body, "persons")
    ids = {p.get("id") for p in persons if isinstance(p, dict)} if isinstance(
        persons, list) else set()
    assert pid in ids, f"persons {sorted(map(str, ids))} do not hold the persona {pid}"
    _expect_memory()(result, world)


# ------------------------------------------------------------------ cases


def _mid(w):
    return {"mid": w.ids["memory"]}


def _title(w):
    return w.names["memory_title"]


MEMORY = ("memory",)
MISSING_MID = {"mid": NO_SUCH_MEMORY}

MEMORIES_CASES = (
    ReadCase(op="readMemory", path_args=_mid, missing=MISSING_MID,
             expect=_expect_memory(), anchors=(_title,), needs=MEMORY),
    ReadCase(op="getMemories", path_args=lambda w: {},
             query=lambda w: {"mids": w.ids["memory"]},
             missing={}, missing_query={"mids": NO_SUCH_MEMORY},
             control_may_be_empty=NO_MATCHING_MIDS_EMPTY,
             expect=_expect_memory(exactly=True), anchors=(_title,), needs=MEMORY),
    ReadCase(op="readMemoryPersonas", path_args=_mid,
             query={"includeTreeIdentifier": True}, missing=MISSING_MID,
             expect=_expect_tree_persona, anchors=(lambda w: w.ids["child"],), needs=MEMORY),
    ReadCase(op="readMemoryPersona",
             path_args=lambda w: {"mid": w.ids["memory"], "pid": w.ids["memory_persona"]},
             missing={"pid": NO_SUCH_PERSONA}, expect=_expect_persona,
             anchors=(_title,), needs=MEMORY),
)
MEMORIES = reads_recipe("reads.memories", MEMORIES_CASES)

USER_MEMORIES_CASES = (
    ReadCase(op="readUserMemories", path_args=lambda w: {"uid": "current"},
             query={"start": 0, "count": 50},
             missing={"uid": NO_SUCH_USER}, control_may_be_empty=NO_USER_MEMORIES_EMPTY,
             expect=_expect_memory(), anchors=(_title,), needs=MEMORY,
             eventual=MEMORY_EVENTUAL),
)
USER_MEMORIES = reads_recipe("reads.memories.user", USER_MEMORIES_CASES)

READS_MISC = (MEMORIES, USER_MEMORIES)
