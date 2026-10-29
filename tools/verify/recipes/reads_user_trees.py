"""Read recipes for the User Tree API (Task C): a group, by id.

The ``ugroup`` world group is a ZZSPIKE group made by ``FixtureSet.user_object`` (the account has none: ``GET /groups`` answers
204). Each read sends only the id and anchors on the fixture's name. The not-found control
is a well-formed unknown id. The list reads and the tree's person/change/match feeds are in
``DEFERRED`` with reasons; the account's current tree is never read for content nor set.
"""
from __future__ import annotations

from ..fixtures import same_id
from ..reads import ReadCase, json_path, reads_recipe
from .user_tree_common import NO_SUCH_USER_OBJECT

DEFERRED: dict[str, str] = {
    "readTree": (
        "no user tree can be created, so none exists to read: beta refuses 17 tried request shapes: groupAccess forms -> 400 'Failed to read request'; others -> 'A user tree must be associated with one and only one group'; the Group/Tree body schema is undocumented (2026-10-29 live run)"),
    "getGroups": (
        "takes no path or query parameter, so no not-found control can be built (every "
        "request lists the signed-in user's groups); the account has none (204) and the "
        "Genealogies run showed a parameterless list can answer public records instead of "
        "the user's own; the group write recipes read by id (readGroup) instead"),
    "readCurrentTree": (
        "takes no path or query parameter, so no not-found control can be built; it always "
        "answers the account's current tree (live: GLOBAL, which no fixture may change), so "
        "no fixture value can anchor it"),
    "readUserTreePersons": (
        "a fresh ZZSPIKE user tree has no persons (the spec documents 204 for an empty "
        "tree), no operation verified so far puts a person in a user tree, and a 204 "
        "carries no anchor the request did not send; not probed live"),
    "readTreeChanges": (
        "a fresh ZZSPIKE user tree has an empty change history (the spec documents 204), "
        "which carries no anchor the request did not send, and no operation verified so far "
        "changes a user tree's content; not probed live"),
    "readTreeMatches": (
        "a fresh ZZSPIKE user tree has no persons and so no matches (the spec documents "
        "204 'no matches found'); a 204 carries no anchor the request did not send; not "
        "probed live"),
}


def _expect(envelope: str, id_key: str, name_key: str):
    def expect(result, world):
        oid, want = world.ids[id_key], world.names[name_key]
        entries = [e for e in json_path(result.body, envelope)
                   if isinstance(e, dict) and e.get("id") and same_id(str(e["id"]), oid)]
        assert len(entries) == 1, f"{envelope} does not hold {oid} once"
        assert entries[0].get("name") == want, (
            f"{oid} is named {entries[0].get('name')!r}, not {want!r}")
    return expect


GROUP = reads_recipe("reads.usertree.group", (
    ReadCase(op="readGroup", path_args=lambda w: {"groupid": w.ids["ugroup"]},
             missing={"groupid": NO_SUCH_USER_OBJECT},
             expect=_expect("groups", "ugroup", "ugroup_name"),
             anchors=(lambda w: w.names["ugroup_name"],), needs=("ugroup",)),))
READS_USER_TREES = (GROUP,)
