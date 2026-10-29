"""A stateful fake of beta's User Tree API (groups and user trees) for the Task C tests.

Shapes are the recipes' guesses: ``{"groups": [{"id", "name", "description"}]}`` and
``{"trees": [...]}``; a create answers 201 with Location and X-ENTITY-ID; an unknown id
answers 400/404 (``unknown_status``); a deleted object 410. Live facts modelled: ``GET
/groups`` answers 204 when there are none, ``GET /trees/current`` answers
``{"trees": [{"id": "GLOBAL"}]}``. ``POST /trees/current`` is recorded in ``current_posts``
and changes the current tree: the safety tests assert it is never called.

``mode`` is None (honest) or one lie, caught by the recipe beside it. ``calls`` lists
``(method, path)``; ``live()`` the objects created and not deleted.
"""
import json
import re
from urllib.parse import unquote, urlsplit

import responses

from familysearch.auth import Environment

BETA = Environment.BETA.base_url
P = BETA + "/platform"
FS_JSON = "application/x-fs-v1+json"

LIES = (
    "read_other",          # every read answers another name
    "catch_all",           # every read answers 200 for any id
    "ignore_create",       # create answers 201, stores nothing
    "wrong_name",          # the stored name is not the posted one
    "ignore_update",
    "update_creates",      # an update makes a new object
    "update_collateral",   # an update also renames the other object
    "update_drops_description",
    "delete_noop",
    "delete_collateral",   # a delete also deletes the other object
    "drop_group",          # a tree reads back without its group
)
NEED_GROUP = "A user tree must be associated with one and only one group"
ENVELOPE = {"groups": "group", "trees": "tree"}


class FakeUserTree:
    def __init__(self, mode=None, *, unknown_status=400, group_of=None):
        if mode is not None and mode not in LIES:
            raise ValueError(f"unknown lie {mode!r}")
        self.mode = mode
        self.unknown_status = unknown_status
        # the one shape of a tree that names its group: here groupAccess as {"groupId": g}
        self.group_of = group_of or (lambda item: (item.get("groupAccess") or {}).get("groupId")
                                     if isinstance(item.get("groupAccess"), dict) else None)
        self.objects = {"groups": {}, "trees": {}}
        self.current = "GLOBAL"
        self.current_posts: list = []
        self.calls: list[tuple[str, str]] = []
        self._seq = 0

    def _path(self, request):
        return unquote(urlsplit(request.url).path)

    def _new_id(self, kind):
        self._seq += 1
        return f"{'UG' if kind == 'groups' else 'UT'}{self._seq:03d}-ZZZ"

    def live(self):
        return [i for kind in self.objects.values() for i, o in kind.items()
                if not o["deleted"]]

    @staticmethod
    def _json(status, body):
        return status, {"Content-Type": FS_JSON}, json.dumps(body)

    def _entry(self, kind, oid):
        o = self.objects[kind][oid]
        name = "ZZSPIKE other" if self.mode == "read_other" else o["name"]
        entry = {"id": oid, "name": name, **{k: v for k, v in o.items()
                                             if k not in ("name", "deleted")}}
        if self.mode == "drop_group":
            entry.pop("groupAccess", None)
        return {kind: [entry]}

    def collection(self, request, kind):
        self.calls.append((request.method, self._path(request)))
        if request.method == "GET":
            live = [i for i, o in self.objects[kind].items() if not o["deleted"]]
            if not live:
                return 204, {}, ""
            return self._json(200, {kind: [self._entry(kind, i)[kind][0] for i in live]})
        item = json.loads(request.body)[kind][0]
        assert item["name"].startswith("ZZSPIKE")
        if kind == "trees":
            gid = self.group_of(item)
            gid = gid if isinstance(gid, str) else None
            live = self.objects["groups"].get(gid)
            if not live or live["deleted"]:
                return 400, {}, NEED_GROUP
        oid = self._new_id(kind)
        if self.mode != "ignore_create":
            rest = {k: v for k, v in item.items() if k != "name"}
            self.objects[kind][oid] = {
                "name": "ZZSPIKE wrong" if self.mode == "wrong_name" else item["name"],
                "deleted": False, **rest}
        return 201, {"Location": f"{P}/{kind}/{oid}", "X-ENTITY-ID": oid}, ""

    def one(self, request, kind):
        self.calls.append((request.method, self._path(request)))
        oid = self._path(request).rsplit("/", 1)[-1]
        if oid == "current":
            return self.current_route(request)
        if self.mode == "catch_all" and request.method == "GET":
            first = next(iter(self.objects[kind]))
            return self._json(200, self._entry(kind, first))
        obj = self.objects[kind].get(oid)
        if obj is None:
            return self.unknown_status, {}, ""
        if obj["deleted"]:
            return 410, {}, ""
        if request.method == "GET":
            return self._json(200, self._entry(kind, oid))
        if request.method == "DELETE":
            if self.mode != "delete_noop":
                obj["deleted"] = True
            if self.mode == "delete_collateral":
                for o in self.objects[kind].values():
                    o["deleted"] = True
            return 204, {}, ""
        item = json.loads(request.body)[kind][0]
        if self.mode == "ignore_update":
            return 204, {}, ""
        if self.mode == "update_creates":
            new = self._new_id(kind)
            self.objects[kind][new] = {**obj, "name": item["name"]}
            return 204, {"Location": f"{P}/{kind}/{new}"}, ""
        obj["name"] = item["name"]
        if self.mode == "update_drops_description":
            obj.pop("description", None)
        if self.mode == "update_collateral":
            for other, o in self.objects[kind].items():
                if other != oid:
                    o["name"] = "ZZSPIKE collateral"
        return 204, {}, ""

    def current_route(self, request):
        if request.method == "POST":
            self.current_posts.append(request.body)
            self.current = json.loads(request.body)["trees"][0]["id"]
            return 204, {}, ""
        return self._json(200, {"trees": [{"id": self.current}]})

    def serve(self):
        for kind in ("groups", "trees"):
            for method in (responses.GET, responses.POST):
                responses.add_callback(
                    method, re.compile(re.escape(f"{P}/{kind}") + r"(\?.*)?$"),
                    callback=lambda r, k=kind: self.collection(r, k))
            for method in (responses.GET, responses.POST, responses.DELETE):
                responses.add_callback(
                    method, re.compile(re.escape(f"{P}/{kind}/") + r"[^/?]+(\?.*)?$"),
                    callback=lambda r, k=kind: self.one(r, k))
        return self
