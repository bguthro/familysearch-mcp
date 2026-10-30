"""Find candidates for unlinked deceased people by walking the FamilySearch tree outward
from linked people. Read-only; pure given a reader (no network here)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .linking import FsPerson, compare
from .planner import current_link
from .prod_reader import ReadFailed
from .record import record_state_and_status
from .scan import Person

_RANK = {"confirmed": 0, "probable": 1}


@dataclass(frozen=True)
class Evidence:
    relationship: str  # the candidate's relationship to the linked person, e.g. "parent", "child-of-parent"
    via_fs_id: str  # the FamilySearch person the candidate was reached from
    via_pkey: str  # "<line>/<folder>" of the linked archive person the walk started from


@dataclass(frozen=True)
class Candidate:
    fs: FsPerson
    verdict: str
    evidence: tuple[Evidence, ...]
    taken_by: str | None


@dataclass(frozen=True)
class TargetResult:
    pkey: str
    candidates: tuple[Candidate, ...]
    ambiguous: bool


@dataclass(frozen=True)
class RelativesResult:
    targets: tuple[TargetResult, ...]
    reads: int
    partial: bool
    living_skipped: int
    failed_reads: int = 0
    starts: int = 0


def _pkey(p: Person) -> str:
    return f"{p.line}/{p.folder}"


def _line_of(pkey: str) -> str:
    return pkey.split("/", 1)[0]


def resolve_through_relatives(
    people: Sequence[Person],
    links: Mapping[str, str | None],
    approved: Mapping[str, str],
    reader,
    *,
    max_hops: int = 2,
    max_reads: int = 600,
    record_status: Callable[[Person], tuple[str, str]] = record_state_and_status,
) -> RelativesResult:
    deceased = [p for p in people if record_status(p)[0] == "deceased"]
    linked = {_pkey(p): current_link(_pkey(p), approved, links) for p in people}
    taken: dict[str, str] = {}
    for key in sorted(k for k, v in linked.items() if v):
        taken.setdefault(linked[key], key)  # type: ignore[arg-type]
    starts = sorted(((_pkey(p), linked[_pkey(p)]) for p in deceased if linked[_pkey(p)]))
    targets = [p for p in deceased if not linked[_pkey(p)]]

    cache: dict[str, list] = {}
    reads = failed = 0
    partial = False
    living_ids: set[str] = set()
    found: dict[str, FsPerson] = {}
    evidence: dict[str, list[Evidence]] = {}

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
            partial = True  # an unread node means the walk is incomplete
            cache[fs_id] = []
        return cache[fs_id]

    # frontier per start: (node fs id, chain from the linked person; "" at the start itself)
    state = {key: {"seen": {fs_id}, "frontier": [(fs_id, "")]} for key, fs_id in starts}
    for hop in range(1, max_hops + 1):
        for key, fs_id in starts:
            st = state[key]
            level: dict[str, str] = {}  # id -> chain, first path wins for expansion
            for node, chain in st["frontier"]:
                rels = read(node)
                if rels is None:
                    continue
                for r in rels:
                    rid = r.fs.id
                    if r.fs.living is not False:  # unknown counts as living: never named
                        living_ids.add(rid)
                        continue
                    if rid in st["seen"]:
                        continue
                    path = r.relationship if not chain else f"{r.relationship}-of-{chain}"
                    ev = Evidence(path, node, key)
                    if ev not in evidence.setdefault(rid, []):
                        evidence[rid].append(ev)
                    found.setdefault(rid, r.fs)
                    level.setdefault(rid, path)
            st["seen"].update(level)
            st["frontier"] = sorted(level.items())

    results = []
    for t in sorted(targets, key=_pkey):
        key = _pkey(t)
        cands = []
        for rid, fs in found.items():
            verdict = compare(t, fs)
            if verdict not in _RANK:
                continue
            ev = tuple(evidence[rid])
            cands.append(Candidate(fs, verdict, ev, taken.get(rid)))

        def rank(c: Candidate):
            same_line = any(_line_of(e.via_pkey) == t.line for e in c.evidence)
            return (_RANK[c.verdict], 0 if same_line else 1, -len(c.evidence))

        cands.sort(key=lambda c: (rank(c), c.fs.id))
        ambiguous = len(cands) > 1 and rank(cands[0]) == rank(cands[1])
        results.append(TargetResult(key, tuple(cands), ambiguous))
    return RelativesResult(tuple(results), reads, partial, len(living_ids), failed, len(starts))
