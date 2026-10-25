"""Resolve the harvested snapshot plus corrections into generator-ready operations.

``overrides.json`` records known defects in the official spec. ``resolve``
applies them to ``snapshot.json`` and returns :class:`ResolvedOp` records, each
with a fingerprint that changes whenever anything a generator would emit does.
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
OVERRIDES_PATH = _HERE / "overrides.json"
SNAPSHOT_PATH = _HERE / "snapshot.json"

MODULES = {
    "Family Tree Endpoints": "family_tree",
    "Standards Endpoints": "standards",
    "Genealogies Endpoints": "genealogies",
    "Memories Endpoints": "memories",
    "User Tree Endpoints": "user_tree",
    "User Endpoints": "users",
    "Utilities Endpoints": "utilities",
    "Authentication Endpoints": "authentication",
}
_OVERRIDE_KEYS = {
    "schema", "exclude", "rename", "path_fix", "kind", "required_query",
    "body_kind", "destructive", "notes",
}
_READ_METHODS = ("GET", "HEAD", "OPTIONS")
_DESTRUCTIVE = re.compile(r"delete|merge|restore", re.I)
_PATH_PARAM = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class ResolvedOp:
    source_operation_id: str
    operation_id: str
    method_name: str
    area: str
    module: str
    http_method: str
    path: str
    summary: str
    kind: str
    destructive: bool
    path_params: tuple[str, ...]
    required_query: tuple[str, ...]
    accepts: tuple[str, ...]
    body: str
    fingerprint: str


def to_snake(operation_id: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", operation_id)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def load_overrides(path: Path = OVERRIDES_PATH) -> dict:
    data = json.loads(Path(path).read_text())
    unknown = set(data) - _OVERRIDE_KEYS
    if unknown:
        raise ValueError(f"unknown override keys: {sorted(unknown)}")
    if data.get("schema") != 1:
        raise ValueError(f"unsupported overrides schema: {data.get('schema')!r}")
    return data


def _fingerprint(fields: dict) -> str:
    blob = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _check_targets(ov: dict, name: str, targets, valid: set) -> None:
    missing = sorted(set(targets) - valid)
    if missing:
        raise ValueError(f"override {name!r} names unknown operation/path: {missing}")


def resolve(snapshot: dict, overrides: dict) -> tuple[list[ResolvedOp], dict[str, str]]:
    snap_ops = snapshot["operations"]
    ids = {o["operation_id"] for o in snap_ops}
    paths = {o["path"] for o in snap_ops}
    exclude = overrides.get("exclude", {})
    rename = overrides.get("rename", {})
    path_fix = overrides.get("path_fix", {})
    kinds = overrides.get("kind", {})
    req_query = overrides.get("required_query", {})
    body_kinds = overrides.get("body_kind", {})
    destructive = overrides.get("destructive", {})
    add, remove = destructive.get("add", []), destructive.get("remove", [])

    for name, targets in (
        ("exclude", exclude), ("rename", rename), ("kind", kinds),
        ("required_query", req_query), ("body_kind", body_kinds),
        ("destructive.add", add), ("destructive.remove", remove),
    ):
        _check_targets(overrides, name, targets, ids)
    _check_targets(overrides, "path_fix", path_fix, paths)

    excluded = {}
    out = []
    for o in snap_ops:
        sid = o["operation_id"]
        if sid in exclude:
            excluded[sid] = exclude[sid]
            continue
        method = o["method"]
        kind = kinds.get(sid, "read" if method in _READ_METHODS else "write")
        if kind not in ("read", "write"):
            raise ValueError(f"{sid}: kind must be 'read' or 'write', got {kind!r}")
        summary = o.get("summary") or ""
        is_destructive = kind == "write" and bool(_DESTRUCTIVE.search(summary))
        if sid in add:
            is_destructive = True
        if sid in remove:
            is_destructive = False
        if method in _READ_METHODS or method == "DELETE":
            body = "none"
        else:
            body = "json"
        body = body_kinds.get(sid, body)
        path = path_fix.get(o["path"], o["path"])
        accepts = ()
        queries = []
        for p in o["params"]:
            if p["in"] == "header" and p["name"].lower() == "accept":
                accepts = tuple(p.get("enum") or ())
            elif p["in"] == "query" and p["required"]:
                queries.append(p["name"])
        for q in req_query.get(sid, []):
            if q not in queries:
                queries.append(q)
        operation_id = rename.get(sid, sid)
        fields = {
            "source_operation_id": sid,
            "operation_id": operation_id,
            "method_name": to_snake(operation_id),
            "area": o["area"],
            "module": MODULES[o["area"]],
            "http_method": method,
            "path": path,
            "summary": summary,
            "kind": kind,
            "destructive": is_destructive,
            "path_params": tuple(_PATH_PARAM.findall(path)),
            "required_query": tuple(queries),
            "accepts": accepts,
            "body": body,
        }
        fp = _fingerprint({k: list(v) if isinstance(v, tuple) else v for k, v in fields.items()})
        out.append(ResolvedOp(**fields, fingerprint=fp))

    out.sort(key=lambda r: (r.module, r.operation_id))
    seen: dict[str, str] = {}
    for r in out:
        if r.method_name in seen:
            raise ValueError(
                f"method_name collision: {r.method_name!r} "
                f"({seen[r.method_name]} and {r.source_operation_id})"
            )
        seen[r.method_name] = r.source_operation_id
    return out, excluded
