"""Reviewed relations files: one JSON per line, the source of nodes and edges for relgap.

Pure and offline. The file is Ben's private archive data; it is validated here and turned into
the same ParsedTree structures the regex parser produces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .treeparse import Edge, Node, ParsedTree

STATUSES = ("deceased", "living", "unknown")
KINDS = ("parent-child", "spouse")


class RelationsError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class RNode:
    id: str
    name: str
    birth: int | None
    death: int | None
    status: str
    folder: str | None
    source: str
    deceased_note: str | None = None


@dataclass(frozen=True)
class REdge:
    kind: str
    a: str
    b: str
    order: int | None
    marriage_year: int | None


@dataclass(frozen=True)
class RelationsFile:
    line: str
    reviewed: bool
    nodes: tuple[RNode, ...]
    edges: tuple[REdge, ...]


def _int_or_none(v) -> bool:
    return v is None or (isinstance(v, int) and not isinstance(v, bool))


def load_relations(path: Path, valid_folders: set[str]) -> RelationsFile:
    path = Path(path)
    name = path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RelationsError([f"{name}: cannot read as JSON ({type(e).__name__})"]) from e
    errors: list[str] = []

    def err(where: str, rule: str) -> None:
        errors.append(f"{name}: {where}: {rule}")

    if not isinstance(data, dict):
        raise RelationsError([f"{name}: top level must be an object"])
    if data.get("version") != 1:
        err("version", "must be 1")
    line = data.get("line")
    if not isinstance(line, str) or not line:
        err("line", "must be a non-empty string")
    reviewed = data.get("reviewed")
    if not isinstance(reviewed, bool):
        err("reviewed", "must be true or false")
    raw_nodes, raw_edges = data.get("nodes"), data.get("edges")
    if not isinstance(raw_nodes, list):
        err("nodes", "must be a list")
        raw_nodes = []
    if not isinstance(raw_edges, list):
        err("edges", "must be a list")
        raw_edges = []

    nodes: list[RNode] = []
    ids: set[str] = set()
    for i, n in enumerate(raw_nodes):
        where = f"nodes[{i}]"
        if not isinstance(n, dict):
            err(where, "must be an object")
            continue
        nid, nm = n.get("id"), n.get("name")
        if not isinstance(nid, str) or not nid:
            err(where, "id must be a non-empty string")
        elif nid in ids:
            err(where, f"duplicate id {nid!r}")
        else:
            ids.add(nid)
        if not isinstance(nm, str) or not nm.strip():
            err(where, "name must be a non-empty string")
        birth, death = n.get("birth"), n.get("death")
        for field, v in (("birth", birth), ("death", death)):
            if not _int_or_none(v):
                err(where, f"{field} must be an integer or null")
        status = n.get("status")
        if status not in STATUSES:
            err(where, "status must be deceased, living or unknown")
        folder = n.get("folder")
        if folder is not None and (not isinstance(folder, str) or folder not in valid_folders):
            err(where, "folder is not an existing person folder")
        source = n.get("source")
        if not isinstance(source, str):
            err(where, "source must be a string (the verbatim diagram or list text)")
        note = n.get("deceased_note")
        if note is not None and not isinstance(note, str):
            err(where, "deceased_note must be a string")
            note = None
        if status == "deceased" and _int_or_none(death) and death is None and not note:
            err(where, "deceased needs a death year or a deceased_note")
        if (status == "deceased" and _int_or_none(birth) and isinstance(birth, int) and birth >= 1900
                and death is None and not note):
            err(where, "born 1900 or later with no death year cannot be deceased without a deceased_note")
        nodes.append(RNode(nid if isinstance(nid, str) else "", nm if isinstance(nm, str) else "",
                           birth if _int_or_none(birth) else None, death if _int_or_none(death) else None,
                           status if status in STATUSES else "unknown",
                           folder if isinstance(folder, str) else None,
                           source if isinstance(source, str) else "", note))

    edges: list[REdge] = []
    for i, e in enumerate(raw_edges):
        where = f"edges[{i}]"
        if not isinstance(e, dict):
            err(where, "must be an object")
            continue
        kind, a, b = e.get("kind"), e.get("a"), e.get("b")
        if kind not in KINDS:
            err(where, "kind must be parent-child or spouse")
        for field, v in (("a", a), ("b", b)):
            if v not in ids:
                err(where, f"{field} does not reference an existing node id")
        if a is not None and a == b:
            err(where, "a and b must differ")
        for field in ("order", "marriage_year"):
            if not _int_or_none(e.get(field)):
                err(where, f"{field} must be an integer or null")
        edges.append(REdge(kind if kind in KINDS else "spouse", a if isinstance(a, str) else "",
                           b if isinstance(b, str) else "",
                           e.get("order") if _int_or_none(e.get("order")) else None,
                           e.get("marriage_year") if _int_or_none(e.get("marriage_year")) else None))
    if errors:
        raise RelationsError(errors)
    return RelationsFile(line, reviewed, tuple(nodes), tuple(edges))  # type: ignore[arg-type]


def _years_agree(a: int | None, b: int | None) -> bool:
    return a is None or b is None or abs(a - b) <= 2


def cross_check(rel: RelationsFile, regex_tree: ParsedTree) -> list[str]:
    """Deceased people the regex parser found in the diagram that the file lacks (name + years)."""
    from .linking import name_matches

    missing: list[str] = []
    for n in regex_tree.nodes:
        if n.death is None and not n.deceased:
            continue
        if any(name_matches(n.name, r.name) and _years_agree(n.birth, r.birth) and _years_agree(n.death, r.death)
               for r in rel.nodes):
            continue
        missing.append(f"{n.name} ({n.birth or '?'}-{n.death or '?'})")
    return missing


def to_parsed_tree(rel: RelationsFile) -> ParsedTree:
    index = {n.id: i for i, n in enumerate(rel.nodes)}
    nodes = tuple(
        Node(rel.line, i, n.source, n.name, n.birth, n.death, False, n.status == "deceased",
             (n.deceased_note,) if n.deceased_note else (), n.folder, n.status)
        for i, n in enumerate(rel.nodes)
    )
    edges = tuple(
        Edge(e.kind, index[e.a], index[e.b], e.order,
             f"m. {e.marriage_year}" if e.kind == "spouse" and e.marriage_year else "")
        for e in rel.edges
    )
    return ParsedTree(nodes, edges, ())
