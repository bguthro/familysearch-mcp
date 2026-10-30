"""Report for the relatives resolver. No network here; nothing is auto-accepted."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .linking import differences
from .linkreport import _span
from .relatives import Candidate, Evidence, RelativesResult
from .scan import Person


def _pkey(p: Person) -> str:
    return f"{p.line}/{p.folder}"


def _evidence_line(e: Evidence) -> str:
    line, _, folder = e.via_pkey.partition("/")
    return f"{e.relationship} of {folder} ({line} / {folder}) via {e.via_fs_id}"


def _flags(c: Candidate) -> list[str]:
    return [f"taken by {c.taken_by} (possible duplicate in the archive)"] if c.taken_by else []


def _approval_line(pkey: str, fs_id: str) -> str:
    return f"{json.dumps(pkey, ensure_ascii=False)}: {json.dumps(fs_id)}"


def render_relatives_report(result: RelativesResult, people: Sequence[Person] = ()) -> str:
    by_key = {_pkey(p): p for p in people}
    found = [t for t in result.targets if t.candidates]
    none = [t for t in result.targets if not t.candidates]
    out = ["# Relatives Report", "",
           "Read-only. Candidates come from relatives of already-linked people. "
           "Nothing here is auto-accepted.", ""]
    if result.partial:
        out += [f"WARNING: PARTIAL RESULT. The walk stopped early ({result.reads} reads, "
                f"{result.failed_reads} failed). Missing candidates below do not mean no match.", ""]
    out += ["## Totals", "",
            f"- starting points (deceased, linked): {result.starts}",
            f"- targets (deceased, unlinked): {len(result.targets)}",
            f"- targets with candidates: {len(found)}",
            f"- targets with no candidate: {len(none)}",
            f"- FamilySearch reads: {result.reads}",
            f"- living relatives skipped (counted only, never listed): {result.living_skipped}", ""]
    if found:
        out += [f"## Candidates ({len(found)})", ""]
    for t in found:
        out.append(f"### {t.pkey}" + (" (ambiguous: top candidates rank equally)" if t.ambiguous else ""))
        person = by_key.get(t.pkey)
        for c in t.candidates:
            flags = _flags(c)
            head = f"- {c.fs.id}: {c.fs.name or '(no name)'} {_span(c.fs)}; {c.verdict}"
            if flags:
                head += "; " + "; ".join(flags)
            out.append(head)
            if person is not None:
                out += [f"  - {d}" for d in differences(person, c.fs)]
            out += [f"  - {_evidence_line(e)}" for e in c.evidence]
            out.append(f"  - to accept, add to approvals.json approved_links: {_approval_line(t.pkey, c.fs.id)}")
        out.append("")
    if none:
        out += [f"## No candidate ({len(none)})", ""]
        out += [f"- {t.pkey}" for t in none]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def relatives_json(result: RelativesResult) -> dict:
    return {
        "partial": result.partial,
        "reads": result.reads,
        "failed_reads": result.failed_reads,
        "living_skipped": result.living_skipped,
        "starts": result.starts,
        "targets": [
            {
                "person": t.pkey,
                "ambiguous": t.ambiguous,
                "candidates": [
                    {
                        "id": c.fs.id,
                        "name": c.fs.name,
                        "birth_year": c.fs.birth_year,
                        "death_year": c.fs.death_year,
                        "verdict": c.verdict,
                        "taken_by": c.taken_by,
                        "evidence": [
                            {"relationship": e.relationship, "via_fs_id": e.via_fs_id, "via_person": e.via_pkey}
                            for e in c.evidence
                        ],
                    }
                    for c in t.candidates
                ],
            }
            for t in result.targets
        ],
    }
