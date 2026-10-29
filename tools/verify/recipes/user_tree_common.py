"""Read-backs shared by the User Tree / groups recipes (Task C).

Groups (``/groups``) and user trees (``/trees``) are read by id (readGroup, readTree):
the list reads (getGroups, readCurrentTree) take no parameter, so no read answer can be
told from a catch-all and the Genealogies runs showed such a list may not be the user's
own objects at all. The shapes are guesses (``{"groups": [{"id", "name", ...}]}``,
``{"trees": [...]}``); every parser fails with the response skeleton
(``describe_box``) when what it needs is absent.

The account's current tree (``GLOBAL``) is never touched: no recipe claims
``setCurrentTree`` and the fixtures never POST ``/trees/current``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from familysearch.api._runtime import ApiResult

from ..core import VerifyContext
from ..fixtures import same_id
from .writes_common import describe_box

# Well-formed in length, but no group or tree: beta should answer 400/404/410.
NO_SUCH_USER_OBJECT = "ZZZZ-ZZZ"
_VOLATILE = ("links", "attribution", "contributor")


@dataclass(frozen=True)
class UserObject:
    """A group or user tree as read by id: its name, and the rest of the entry."""

    id: str
    name: str | None
    data: str = field(compare=True, default="")  # the entry as JSON, links left out
    result: ApiResult = field(compare=False, repr=False, default=None)

    @property
    def description(self):
        return json.loads(self.data).get("description")

    def other_than_name(self) -> str:
        entry = json.loads(self.data)
        entry.pop("name", None)
        return json.dumps(entry, sort_keys=True)


def user_object(result: ApiResult, envelope: str, oid: str) -> UserObject:
    """The entry of ``envelope`` that is ``oid`` (ids compared with their prefix dropped)."""
    body = result.body if isinstance(result.body, dict) else {}
    found = [e for e in body.get(envelope) or []
             if isinstance(e, dict) and e.get("id") and same_id(str(e["id"]), oid)]
    if len(found) != 1:
        raise AssertionError(f"the read of {envelope[:-1]} {oid} does not hold it once; "
                             f"{describe_box(result)}")
    entry = {k: v for k, v in found[0].items() if k not in _VOLATILE}
    return UserObject(oid, entry.get("name"), json.dumps(entry, sort_keys=True), result)


def read_group(ctx: VerifyContext, gid: str) -> UserObject:
    return user_object(ctx.api.read_group(gid), "groups", gid)


def read_user_tree(ctx: VerifyContext, tid: str) -> UserObject:
    return user_object(ctx.api.read_tree(tid), "trees", tid)
