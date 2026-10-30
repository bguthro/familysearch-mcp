"""Report for the relationship gap evaluation. No network here; nothing is proposed for writing
except lines Ben may paste into approvals.json. Living people appear as counts only."""

from __future__ import annotations

import json
import re

from .relgap import EdgeResult, RelGapResult

STATUSES = ("conflict", "missing-in-familysearch", "blocked", "present", "skipped-living")
_YEAR_RE = re.compile(r"\b(\d{4})\b")
_DEATH_RE = re.compile(r"[–-]\s*\d{4}|\bd\.\s*\d{4}")


def edge_key(e: EdgeResult) -> str:
    """Stable key for approvals.json `approved_relationships`: <kind>:<fs_id_a>:<fs_id_b>."""
    a, b = e.a_fs or "", e.b_fs or ""
    if e.kind == "spouse":
        a, b = sorted((a, b))
    return f"{e.kind}:{a}:{b}"


def _may_name_living(raw: str) -> bool:
    years = [int(y) for y in _YEAR_RE.findall(raw)]
    return any(y >= 1900 for y in years) and not _DEATH_RE.search(raw)


def _unparsed_text(raw: str) -> str:
    return "(text withheld: it may name a living person)" if _may_name_living(raw) else raw.strip()


def _label(e: EdgeResult) -> str:
    return f"{e.a_label} -> {e.b_label}" if e.kind == "parent-child" else f"{e.a_label} + {e.b_label} (spouses)"


def _ids(e: EdgeResult) -> str:
    return f"{e.a_fs or '-'} / {e.b_fs or '-'}"


def _edge_lines(e: EdgeResult) -> list[str]:
    out = [f"- {_label(e)} [{_ids(e)}] ({', '.join(e.lines)})" + (f": {e.note}" if e.note else "")]
    if e.fs_found:
        out.append(f"  - FamilySearch has: {', '.join(e.fs_found)}")
    if e.approved_ends:
        out.append(f"  - link approved in approvals.json: {', '.join(e.approved_ends)}")
    return out


def render_relgap_report(result: RelGapResult) -> str:
    counts = {s: sum(1 for e in result.edges if e.status == s) for s in STATUSES}
    out = ["# Relationship Report", "",
           "Read-only. FamilySearch wins: conflicts are never offered for adding. "
           "Nothing here is written to FamilySearch.", ""]
    if result.partial:
        out += [f"WARNING: PARTIAL RESULT. Reads stopped early ({result.reads} reads, "
                f"{result.failed_reads} failed). Blocked edges below were not checked.", ""]
    out += ["## Totals", ""]
    out += [f"- {s}: {counts[s]}" for s in STATUSES if s != "skipped-living"]
    out += [f"- creation candidates (deceased, no folder or no link): {len(result.create_candidates)}",
            f"- unmapped or ambiguous diagram nodes: {len(result.unmapped_nodes)}",
            f"- unparsed diagram lines: {len(result.unparsed_lines)}",
            f"- FamilySearch reads: {result.reads}",
            f"- living people skipped (counted only, never listed): edges {result.living_skipped}, "
            f"nodes {result.living_nodes}", ""]
    titles = {
        "conflict": "Conflicts (FamilySearch already differs; not proposed)",
        "missing-in-familysearch": "Missing in FamilySearch",
        "blocked": "Blocked",
        "present": "Present",
    }
    for s in ("conflict", "missing-in-familysearch", "blocked", "present"):
        rows = [e for e in result.edges if e.status == s]
        if not rows:
            continue
        out += [f"## {titles[s]} ({len(rows)})", ""]
        for e in rows:
            out += _edge_lines(e)
        out.append("")
    missing = [e for e in result.edges if e.status == "missing-in-familysearch"]
    if missing:
        out += ["## Approval lines (missing-in-familysearch edges)", "",
                "To approve an edge, add its key to approvals.json under approved_relationships:", "",
                '    "approved_relationships": [']
        out += [f"        {json.dumps(edge_key(e))},  // {_label(e)}" for e in missing]
        out += ["    ]", ""]
    if result.create_candidates:
        out += [f"## Creation candidates ({len(result.create_candidates)})", "",
                "Deceased diagram people with no folder or no link. Check the relatives report first.", ""]
        for c in result.create_candidates:
            span = f"{c.birth or '?'}-{c.death or '?'}"
            rel = f"; related to {', '.join(c.related_to)}" if c.related_to else ""
            out.append(f"- {c.label} ({span}); {c.reason}; from the diagram in {', '.join(c.lines)}{rel}")
        out.append("")
    if result.unmapped_nodes:
        out += [f"## Unmapped nodes ({len(result.unmapped_nodes)})", ""]
        out += [f"- {u.label}: {u.status} ({', '.join(u.lines)})" for u in result.unmapped_nodes]
        out.append("")
    if result.unparsed_lines:
        out += [f"## Unparsed diagram lines ({len(result.unparsed_lines)})", ""]
        out += [f"- {line}: {_unparsed_text(raw)}" for line, raw in result.unparsed_lines]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def relgap_json(result: RelGapResult) -> dict:
    return {
        "partial": result.partial,
        "reads": result.reads,
        "failed_reads": result.failed_reads,
        "living_skipped": result.living_skipped,
        "living_nodes": result.living_nodes,
        "edges": [
            {
                "key": edge_key(e),
                "kind": e.kind,
                "a": e.a_label,
                "b": e.b_label,
                "a_fs": e.a_fs,
                "b_fs": e.b_fs,
                "status": e.status,
                "note": e.note,
                "lines": list(e.lines),
                "approved_ends": list(e.approved_ends),
                "fs_found": list(e.fs_found),
            }
            for e in result.edges
        ],
        "create_candidates": [
            {"label": c.label, "birth": c.birth, "death": c.death, "reason": c.reason,
             "lines": list(c.lines), "related_to": list(c.related_to)}
            for c in result.create_candidates
        ],
        "unmapped_nodes": [{"label": u.label, "status": u.status, "lines": list(u.lines)}
                           for u in result.unmapped_nodes],
        "unparsed_lines": [{"line": line, "text": _unparsed_text(raw)} for line, raw in result.unparsed_lines],
    }
