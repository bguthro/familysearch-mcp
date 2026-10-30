"""Match diagram people who have no archive folder to existing FamilySearch records, through the
linked relatives they are connected to in relationships.json. Read-only; pure given a reader.

Only deceased (living is False) FamilySearch persons are considered or named; living ones are counted.
Nothing is applied: matches become links only when Ben lists them under approved_nofolder."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .linking import FsPerson, compare
from .prod_reader import ReadFailed
from .scan import Person

_RANK = {"confirmed": 0, "probable": 1}
_SKIP_LABELS = {"(living)", "(unknown)"}


@dataclass(frozen=True)
class Link:
    role: str  # the candidate's role relative to the linked person: parent | child | spouse
    via_fs_id: str  # the linked FamilySearch person whose relatives were read
    via_label: str  # that person's name in the diagram


@dataclass(frozen=True)
class Match:
    fs: FsPerson
    verdict: str  # confirmed | probable
    evidence: tuple[Link, ...]


@dataclass(frozen=True)
class NoFolderTarget:
    label: str
    birth: int | None
    death: int | None
    verdict: str  # confirmed | probable | ambiguous | not-found
    matches: tuple[Match, ...]
    linked_edges: int  # edges to an end with a FamilySearch id (what the search could use)
    approved_id: str | None = None


@dataclass(frozen=True)
class NoFolderResult:
    targets: tuple[NoFolderTarget, ...]
    reads: int
    partial: bool
    living_skipped: int
    failed_reads: int = 0


def _candidates(rel: Mapping) -> list[tuple[str, int | None, int | None]]:
    out: dict[tuple[str, int | None, int | None], None] = {}
    for c in rel.get("create_candidates") or []:
        if c.get("reason") == "no folder" and c.get("label") not in _SKIP_LABELS:
            out[(c["label"], c.get("birth"), c.get("death"))] = None
    have = {label for label, _, _ in out}
    for e in rel.get("edges") or []:
        if e.get("status") == "skipped-living" or "has no folder" not in (e.get("note") or ""):
            continue
        for side in ("a", "b"):
            label = e.get(side)
            if label and label not in _SKIP_LABELS and not e.get(f"{side}_fs") and not e.get(f"{side}_folder") \
                    and label not in have:
                have.add(label)
                out[(label, None, None)] = None
    return list(out)


def find_nofolder_matches(
    relationships: Mapping,
    reader,
    approved_nofolder: Mapping[str, str] | None = None,
    *,
    max_reads: int = 600,
) -> NoFolderResult:
    approved_nofolder = approved_nofolder or {}
    edges = [e for e in relationships.get("edges") or []
             if e.get("kind") in ("parent-child", "spouse") and e.get("status") != "skipped-living"]
    cache: dict[str, list | None] = {}
    reads = failed = 0
    partial = False
    living_ids: set[str] = set()

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
            cache[fs_id] = None
        return cache[fs_id]

    targets = []
    for label, birth, death in sorted(_candidates(relationships), key=lambda c: (c[0], c[1] or 0, c[2] or 0)):
        probe = Person("", label, Path("/nonexistent"), birth, death, label, (), (), frozenset())
        found: dict[str, tuple[FsPerson, str, list[Link]]] = {}
        linked_edges = 0
        for e in edges:
            for side, other in (("a", "b"), ("b", "a")):
                if e.get(side) != label or not e.get(f"{other}_fs"):
                    continue
                if e["kind"] == "spouse":
                    role = "spouse"
                else:  # a is the parent, b the child: the candidate is the other end's parent or child
                    role = "parent" if side == "a" else "child"
                via = e[f"{other}_fs"]
                linked_edges += 1
                rels = read(via)
                for r in rels or []:
                    if r.relationship != role:
                        continue
                    if r.fs.living is not False:  # unknown counts as living: never named
                        living_ids.add(r.fs.id)
                        continue
                    verdict = compare(probe, r.fs)
                    if verdict not in _RANK:
                        continue
                    link = Link(role, via, e.get(other) or "")
                    _, best, links = found.setdefault(r.fs.id, (r.fs, verdict, []))
                    if link not in links:
                        links.append(link)
        matches = sorted((Match(fs, v, tuple(ls)) for fs, v, ls in found.values()),
                         key=lambda m: (_RANK[m.verdict], -len(m.evidence), m.fs.id))
        if not matches:
            verdict = "not-found"
        elif len(matches) > 1:
            verdict = "ambiguous"
        else:
            verdict = matches[0].verdict
        targets.append(NoFolderTarget(label, birth, death, verdict, tuple(matches), linked_edges,
                                      approved_nofolder.get(label)))
    return NoFolderResult(tuple(targets), reads, partial, len(living_ids), failed)
