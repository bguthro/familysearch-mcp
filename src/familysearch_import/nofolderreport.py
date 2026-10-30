"""Report for the no-folder resolver. No network here; nothing is auto-accepted."""

from __future__ import annotations

import json

from .linkreport import _span
from .nofolder import NoFolderResult

_ORDER = ("confirmed", "probable", "ambiguous", "not-found")


def render_nofolder_report(result: NoFolderResult) -> str:
    by = {v: [t for t in result.targets if t.verdict == v] for v in _ORDER}
    out = ["# No-Folder Report", "",
           "Read-only. People in the family diagrams with no archive folder, matched to FamilySearch "
           "through the linked relatives they connect to. Nothing here is applied automatically.", ""]
    if result.partial:
        out += [f"WARNING: PARTIAL RESULT. The reads stopped early ({result.reads} reads, "
                f"{result.failed_reads} failed). not-found below does not mean no match.", ""]
    out += ["## Totals", "", f"- people without a folder: {len(result.targets)}"]
    out += [f"- {v}: {len(by[v])}" for v in _ORDER]
    out += [f"- FamilySearch reads: {result.reads}",
            f"- living relatives skipped (counted only, never listed): {result.living_skipped}", ""]
    for v in _ORDER[:3]:
        if not by[v]:
            continue
        out += [f"## {v.capitalize()} ({len(by[v])})", ""]
        for t in by[v]:
            span = f"{t.birth or '?'}-{t.death or '?'}"
            note = f"; approved: {t.approved_id}" if t.approved_id else ""
            out.append(f"### {t.label} ({span}){note}")
            for m in t.matches:
                out.append(f"- {m.fs.id}: {m.fs.name or '(no name)'} {_span(m.fs)}; {m.verdict}")
                out += [f"  - {e.role} of {e.via_label or '(unnamed)'} via {e.via_fs_id}" for e in m.evidence]
                out.append(f"  - to accept, add to approvals.json approved_nofolder: "
                           f"{json.dumps(t.label, ensure_ascii=False)}: {json.dumps(m.fs.id)}")
            out.append("")
    if by["not-found"]:
        out += [f"## Not found ({len(by['not-found'])})", ""]
        for t in by["not-found"]:
            out.append(f"- {t.label} ({t.birth or '?'}-{t.death or '?'}); linked edges: {t.linked_edges}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def nofolder_json(result: NoFolderResult) -> dict:
    return {
        "partial": result.partial,
        "reads": result.reads,
        "failed_reads": result.failed_reads,
        "living_skipped": result.living_skipped,
        "targets": [
            {
                "label": t.label,
                "birth": t.birth,
                "death": t.death,
                "verdict": t.verdict,
                "linked_edges": t.linked_edges,
                "approved_id": t.approved_id,
                "matches": [
                    {
                        "id": m.fs.id,
                        "name": m.fs.name,
                        "birth_year": m.fs.birth_year,
                        "death_year": m.fs.death_year,
                        "verdict": m.verdict,
                        "evidence": [{"role": e.role, "via_fs_id": e.via_fs_id, "via_label": e.via_label}
                                     for e in m.evidence],
                    }
                    for m in t.matches
                ],
            }
            for t in result.targets
        ],
    }
