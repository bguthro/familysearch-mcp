"""Compare the relationships in the archive's FamilyTree diagrams with FamilySearch. Read-only.

Pure given a reader (anything with get_relatives(fs_id); no network here). Nothing is proposed
for writing: FamilySearch wins, and living people appear as counts only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .linking import FsPerson, _fold, compare
from .planner import current_link
from .prod_reader import ReadFailed
from .record import record_state_and_status
from .scan import Person
from .treeparse import Node, ParsedTree

Status = Literal["present", "missing-in-familysearch", "conflict", "blocked", "skipped-living"]
LIVING = "(living)"


@dataclass(frozen=True)
class MappedNode:
    person: Person | None
    status: Literal["mapped", "unmapped", "ambiguous"]
    living: bool
    unknown: bool = False  # living is a precaution: no dates and no death marker


@dataclass(frozen=True)
class EdgeResult:
    kind: str
    a_label: str
    b_label: str
    a_fs: str | None
    b_fs: str | None
    status: Status
    note: str
    lines: tuple[str, ...]
    approved_ends: tuple[str, ...] = ()  # labels of ends whose link comes from approvals.json
    fs_found: tuple[str, ...] = ()  # conflict / partial: what FamilySearch has, living redacted
    a_folder: str | None = None  # "<line>/<folder>" of each end's archive folder (mapped ends only)
    b_folder: str | None = None


@dataclass(frozen=True)
class CreateCandidate:
    label: str
    birth: int | None
    death: int | None
    reason: str  # "no folder" | "folder has no link"
    lines: tuple[str, ...]
    related_to: tuple[str, ...]  # deceased linked people it connects to


@dataclass(frozen=True)
class UnmappedNode:
    label: str
    status: str  # unmapped | ambiguous
    lines: tuple[str, ...]


@dataclass(frozen=True)
class RelGapResult:
    edges: tuple[EdgeResult, ...]
    create_candidates: tuple[CreateCandidate, ...]
    unmapped_nodes: tuple[UnmappedNode, ...]
    reads: int
    partial: bool
    living_skipped: int
    unparsed_lines: tuple[tuple[str, str], ...] = ()  # (line, raw diagram text)
    living_nodes: int = 0
    failed_reads: int = 0
    unknown_nodes: int = 0  # no dates, not known deceased: counted, never named
    sources: tuple[tuple[str, str], ...] = ()  # (line, where its nodes and edges came from)


def _pkey(p: Person) -> str:
    return f"{p.line}/{p.folder}"


def _node_state(node: Node) -> tuple[bool, bool]:
    """(living, unknown). Unknown (no dates and no death marker) is held back like living."""
    if node.status == "living":
        return True, False
    if node.status == "unknown":
        return True, True
    if node.death is not None or node.deceased or node.status == "deceased":
        return False, False
    if node.birth is None:
        return True, True
    return node.birth >= 1900, False


def _fits(person: Person, node: Node) -> bool:
    fs = FsPerson("", node.name, node.birth, node.death, None, "ok")
    if compare(person, fs) == "mismatch":
        return False
    return not (person.death is not None and node.death is not None and abs(person.death - node.death) > 2)


def map_nodes(
    parsed_by_line: Mapping[str, ParsedTree],
    people: Sequence[Person],
    record_status: Callable[[Person], tuple[str, str]] = record_state_and_status,
) -> dict[tuple[str, int], MappedNode]:
    out: dict[tuple[str, int], MappedNode] = {}
    by_key = {_pkey(p): p for p in people}
    for line, tree in parsed_by_line.items():
        same = [p for p in people if p.line == line]
        other = [p for p in people if p.line != line]
        for node in tree.nodes:
            key = (line, node.index)
            if node.folder:  # explicit, reviewed mapping: no name matching
                p = by_key.get(node.folder)
                if p is not None:
                    # the file may only add caution: an explicit "living" status wins
                    out[key] = MappedNode(p, "mapped", record_status(p)[0] != "deceased" or node.status == "living")
                    continue
                out[key] = MappedNode(None, "unmapped", *_node_state(node))
                continue
            if node.birth is None and node.death is None:
                living, unknown = _node_state(node)  # nothing to corroborate the name
                out[key] = MappedNode(None, "unmapped", living, unknown)
                continue
            chosen: MappedNode | None = None
            for pool in (same, other):
                fits = [p for p in pool if _fits(p, node)]
                if len(fits) == 1:
                    p = fits[0]
                    chosen = MappedNode(p, "mapped", record_status(p)[0] != "deceased")
                    break
                if len(fits) > 1:
                    chosen = MappedNode(None, "ambiguous", *_node_state(node))
                    break
            out[key] = chosen or MappedNode(None, "unmapped", *_node_state(node))
    return out


@dataclass
class _End:
    ident: tuple
    node: Node
    mapped: MappedNode
    line: str

    @property
    def label(self) -> str:
        if self.mapped.unknown:
            return "(unknown)"
        return LIVING if self.mapped.living else self.node.name


def _ident(line: str, node: Node, m: MappedNode) -> tuple:
    if m.status == "mapped" and m.person is not None:
        return ("p", _pkey(m.person))
    return ("n", _fold(node.name), node.birth, node.death)


def evaluate_edges(
    parsed_by_line: Mapping[str, ParsedTree],
    people: Sequence[Person],
    links: Mapping[str, str | None],
    approved: Mapping[str, str],
    reader,
    *,
    max_reads: int = 600,
    record_status: Callable[[Person], tuple[str, str]] = record_state_and_status,
    sources: Mapping[str, str] | None = None,
) -> RelGapResult:
    mapped = map_nodes(parsed_by_line, people, record_status)

    # 1. unique edges across all lines
    unique: dict[tuple, dict] = {}
    ends: dict[tuple, _End] = {}
    node_lines: dict[tuple, set[str]] = {}
    for line, tree in parsed_by_line.items():
        for e in tree.edges:
            a = _End(_ident(line, tree.nodes[e.a], mapped[(line, e.a)]), tree.nodes[e.a], mapped[(line, e.a)], line)
            b = _End(_ident(line, tree.nodes[e.b], mapped[(line, e.b)]), tree.nodes[e.b], mapped[(line, e.b)], line)
            for end in (a, b):
                ends.setdefault(end.ident, end)
                node_lines.setdefault(end.ident, set()).add(line)
            if e.kind == "spouse":
                key = ("spouse", frozenset((a.ident, b.ident)))
                if a.ident == b.ident:
                    continue
            else:
                key = ("parent-child", a.ident, b.ident)
            rec = unique.setdefault(key, {"kind": e.kind, "a": a, "b": b, "lines": set(), "notes": []})
            rec["lines"].add(line)
            if e.note and e.note not in rec["notes"]:
                rec["notes"].append(e.note)
        for i, n in enumerate(tree.nodes):  # nodes outside any edge still count for candidates
            m = mapped[(line, i)]
            ident = _ident(line, n, m)
            ends.setdefault(ident, _End(ident, n, m, line))
            node_lines.setdefault(ident, set()).add(line)

    def link_of(end: _End) -> str | None:
        p = end.mapped.person
        return current_link(_pkey(p), approved, links) if p is not None and end.mapped.status == "mapped" else None

    # archive parents of each child, for the partial-parents rule
    archive_parents: dict[tuple, list[tuple]] = {}
    for key, rec in unique.items():
        if rec["kind"] == "parent-child":
            archive_parents.setdefault(rec["b"].ident, []).append(rec["a"].ident)

    cache: dict[str, list] = {}
    reads = failed = 0
    partial = False

    def read(fs_id: str):
        nonlocal reads, failed, partial
        if fs_id in cache:
            return cache[fs_id]
        if reads >= max_reads:
            partial = True
            return None
        reads += 1
        try:
            cache[fs_id] = list(reader.get_relatives(fs_id))
        except ReadFailed:
            failed += 1
            partial = True
            cache[fs_id] = None  # type: ignore[assignment]
        return cache[fs_id]

    def fs_label(r) -> str:
        shown = r.fs.name or "(no name)" if r.fs.living is False else LIVING
        return f"{shown} ({r.fs.id})"

    results: list[EdgeResult] = []
    living_skipped = 0
    def skey(k: tuple):
        return (k[0], tuple(sorted(map(repr, k[1]))) if k[0] == "spouse" else tuple(repr(x) for x in k[1:]))

    for key in sorted(unique, key=skey):
        rec = unique[key]
        a, b, kind = rec["a"], rec["b"], rec["kind"]
        lines = tuple(sorted(rec["lines"]))
        note = "; ".join(rec["notes"])

        def done(status: Status, extra: str = "", a_fs=None, b_fs=None, found=(), appr=()):
            text = "; ".join(x for x in (extra, note) if x)
            def folder(end: _End) -> str | None:
                if end.mapped.living or end.mapped.unknown:  # folder names carry names: never for living people
                    return None
                return _pkey(end.mapped.person) if end.mapped.status == "mapped" and end.mapped.person else None

            results.append(EdgeResult(kind, a.label, b.label, a_fs, b_fs, status, text, lines, appr, found,
                                      folder(a), folder(b)))

        if a.mapped.living or b.mapped.living:
            living_skipped += 1
            done("skipped-living", "an end is living or of unknown status; counted only")
            continue
        names = ("parent", "child") if kind == "parent-child" else ("first spouse", "second spouse")
        blocked = ""
        for end, role in ((a, names[0]), (b, names[1])):
            if end.mapped.status == "unmapped":
                blocked = f"blocked: {role} has no folder"
            elif end.mapped.status == "ambiguous":
                blocked = f"blocked: {role} fits several folders"
            elif link_of(end) is None:
                blocked = f"blocked: {role} has no link"
            if blocked:
                break
        if blocked:
            done("blocked", blocked)
            continue
        fa, fb = link_of(a), link_of(b)
        appr = tuple(e.label for e in (a, b) if approved.get(_pkey(e.mapped.person)))  # type: ignore[arg-type]
        if kind == "spouse":
            rels = read(fa)  # type: ignore[arg-type]
            if rels is None:
                done("blocked", "blocked: " + ("FamilySearch read failed" if fa in cache else "read budget reached"),
                     fa, fb, appr=appr)
                continue
            spouse_ids = {r.fs.id for r in rels if r.relationship == "spouse"}
            if fb in spouse_ids:
                done("present", "", fa, fb, appr=appr)
            else:
                done("missing-in-familysearch", "no couple relationship on FamilySearch", fa, fb, appr=appr)
            continue
        rels = read(fb)  # type: ignore[arg-type]
        if rels is None:
            done("blocked", "blocked: " + ("FamilySearch read failed" if fb in cache else "read budget reached"),
                 fa, fb, appr=appr)
            continue
        fs_parents = [r for r in rels if r.relationship == "parent"]
        ids = {r.fs.id for r in fs_parents}
        found = tuple(fs_label(r) for r in fs_parents)
        if fa in ids:
            done("present", "", fa, fb, appr=appr)
        elif not fs_parents:
            done("missing-in-familysearch", "child has no parents on FamilySearch", fa, fb, appr=appr)
        else:
            others = [link_of(ends[i]) for i in archive_parents.get(b.ident, []) if i != a.ident and i in ends]
            if len(ids) == 1 and ids & {o for o in others if o}:
                done("missing-in-familysearch", "partial-parents: FamilySearch has only the other parent",
                     fa, fb, found, appr)
            else:
                done("conflict", "FamilySearch already has different parents; FamilySearch wins", fa, fb, found, appr)

    # 2. creation candidates and unmapped nodes (deceased only; living is counted)
    linked_deceased_neighbours: dict[tuple, set[str]] = {}
    for rec in unique.values():
        for x, y in ((rec["a"], rec["b"]), (rec["b"], rec["a"])):
            if not y.mapped.living and y.mapped.status == "mapped" and link_of(y):
                linked_deceased_neighbours.setdefault(x.ident, set()).add(y.node.name)
    cands: list[CreateCandidate] = []
    unmapped: list[UnmappedNode] = []
    living_nodes = unknown_nodes = 0
    for ident, end in sorted(ends.items(), key=lambda kv: (kv[1].node.name, repr(kv[0]))):
        lines = tuple(sorted(node_lines[ident]))
        if end.mapped.unknown:
            unknown_nodes += 1
            continue
        if end.mapped.living:
            living_nodes += 1
            continue
        if end.mapped.status in ("unmapped", "ambiguous"):
            unmapped.append(UnmappedNode(end.node.name, end.mapped.status, lines))
        if end.mapped.status == "ambiguous":
            continue
        reason = "no folder" if end.mapped.status == "unmapped" else (
            "folder has no link" if link_of(end) is None else "")
        if reason:
            cands.append(CreateCandidate(end.node.name, end.node.birth, end.node.death, reason, lines,
                                         tuple(sorted(linked_deceased_neighbours.get(ident, ())))))

    order = {"conflict": 0, "missing-in-familysearch": 1, "blocked": 2, "present": 3, "skipped-living": 4}
    results.sort(key=lambda r: (order[r.status], r.kind, r.a_label, r.b_label))
    unparsed = tuple((ln, raw) for ln, t in parsed_by_line.items() for raw in t.unparsed_lines)
    return RelGapResult(tuple(results), tuple(cands), tuple(unmapped), reads, partial, living_skipped,
                        unparsed, living_nodes, failed, unknown_nodes, tuple(sorted((sources or {}).items())))
