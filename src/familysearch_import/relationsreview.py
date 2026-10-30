"""Human review sheet for relations files. Pure. Living and unknown-status people are never named."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .relations import RelationsFile

LIVING = "(living person)"
UNKNOWN = "(unknown-status person)"


_APPROX = re.compile(r"~|\bc\.|\bca\.?\s*\d|\babt\.?")


def _years(birth: int | None, death: int | None) -> str:
    return f"{birth or '?'}-{death or '?'}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def render_line_review(rel: RelationsFile, folder_years: Mapping[str, tuple[int | None, int | None]]) -> list[str]:
    nodes = {n.id: n for n in rel.nodes}
    visible = {n.id for n in rel.nodes if n.status == "deceased"}
    living = sum(1 for n in rel.nodes if n.status == "living")
    unknown = sum(1 for n in rel.nodes if n.status == "unknown")

    def label(i: str) -> str:
        n = nodes[i]
        return f"{n.name} ({_years(n.birth, n.death)})" if i in visible else (LIVING if n.status == "living" else UNKNOWN)

    hidden_edges = sum(1 for e in rel.edges if e.a not in visible or e.b not in visible)
    out = [f"## {rel.line}", "",
           f"- reviewed: {'yes' if rel.reviewed else 'NO (awaiting your review)'}",
           f"- people: {len(rel.nodes)} (deceased {len(visible)}, {LIVING} {living}, {UNKNOWN} {unknown})",
           f"- relationships: {len(rel.edges)} (touching a living or unknown-status person, counted only: "
           f"{hidden_edges})", ""]

    out += ["### People", "", "| Name | Years | Status | Folder |", "| --- | --- | --- | --- |"]
    for n in rel.nodes:
        if n.id in visible:
            out.append(f"| {n.name} | {_years(n.birth, n.death)} | deceased | {n.folder or 'no folder'} |")
    if living:
        out.append(f"| {LIVING} x {living} | | living | |")
    if unknown:
        out.append(f"| {UNKNOWN} x {unknown} | | unknown | |")
    out.append("")

    parents: dict[str, list[str]] = {}
    spouses: dict[str, list] = {}
    for e in rel.edges:
        if e.kind == "parent-child":
            parents.setdefault(e.b, []).append(e.a)
        else:
            spouses.setdefault(e.a, []).append(e)
            spouses.setdefault(e.b, []).append(e)
    couples = [e for e in rel.edges if e.kind == "spouse" and e.a in visible and e.b in visible]
    placed: set[str] = set()
    out += ["### Relationships", ""]
    for e in couples:
        pair = {e.a, e.b}
        kids = [c for c, ps in parents.items() if set(ps) == pair and len(ps) == 2 and c in visible]
        placed.update(kids)
        bits = []
        if e.marriage_year:
            bits.append(f"m. {e.marriage_year}")
        if e.order:
            bits.append(f"{_ordinal(e.order)} marriage")
        head = f"{label(e.a)} + {label(e.b)}" + (f" ({', '.join(bits)})" if bits else "")
        out.append(f"- {head} -> children: " + (", ".join(label(c) for c in kids) or "none shown"))
    hidden_kids = 0
    for c, ps in parents.items():
        if c not in visible:
            hidden_kids += 1
            continue
        if c in placed:
            continue
        vis = [p for p in ps if p in visible]
        hid = len(ps) - len(vis)
        if len(ps) == 1:
            out.append(f"- {label(c)} (only one parent shown: {label(ps[0])})")
        elif hid:
            out.append(f"- {label(c)} (parents: {', '.join(label(p) for p in vis) or 'none named'}"
                       f" and {hid} living or unknown-status {'person' if hid == 1 else 'people'})")
        else:
            out.append(f"- {label(c)} (parents: {', '.join(label(p) for p in vis)}; not a married pair in this file)")
    out.append(f"- relationships not shown because they touch a living or unknown-status person: {hidden_edges}")
    out.append("")

    away = [n for n in rel.nodes if n.id in visible and n.folder is None]
    out += ["### Not in your archive folders", ""]
    out += [f"- {n.name} ({_years(n.birth, n.death)})" for n in away] or ["- none"]
    out.append("")

    check: list[str] = []
    for n in rel.nodes:
        if n.folder and n.folder in folder_years and n.id in visible:
            fb, fd = folder_years[n.folder]
            approx = bool(_APPROX.search(n.source))
            diffs = [f"{what} {mine} vs folder {theirs}" for what, mine, theirs in
                     (("birth", n.birth, fb), ("death", n.death, fd))
                     if mine is not None and theirs is not None and mine != theirs
                     and not (approx and abs(mine - theirs) == 1)]
            if diffs:
                check.append(f"{n.name}: years differ from the folder name ({'; '.join(diffs)})")
        if n.id in visible and n.birth is not None and n.birth >= 1900 and n.death is None:
            check.append(f"{n.name}: marked deceased but born {n.birth}"
                         + (f" ({n.deceased_note})" if n.deceased_note else ""))
    if unknown:
        check.append(f"{unknown} {UNKNOWN} node(s) have status unknown: decide living or deceased (not named)")
    for c, ps in parents.items():
        if len(ps) > 2:
            check.append(f"{label(c) if c in visible else 'a hidden person'}: has {len(ps)} parents")
    for i, edges in spouses.items():
        if len(edges) > 1 and any(e.order is None for e in edges):
            check.append(f"{label(i) if i in visible else 'a hidden person'}: {len(edges)} spouses but some "
                         "marriages have no order")
    linked = {e.a for e in rel.edges} | {e.b for e in rel.edges}
    lonely = [n for n in rel.nodes if n.id not in linked]
    check += [f"{n.name}: has no relationships" for n in lonely if n.id in visible]
    if any(n.id not in visible for n in lonely):
        check.append(f"{sum(1 for n in lonely if n.id not in visible)} living or unknown-status node(s) "
                     "have no relationships (not named)")
    out += ["### Review checklist", ""] + ([f"- {c}" for c in check] or ["- nothing unusual"]) + [""]
    return out


def render_review(
    entries: Sequence[tuple[str, RelationsFile | list[str]]],
    folder_years: Mapping[str, tuple[int | None, int | None]],
) -> str:
    out = ["# Relations Review", "",
           "Generated for you to read before signing off with `relations-approve`. Living and "
           "unknown-status people are never named here.", ""]
    for name, item in entries:
        if isinstance(item, list):
            out += [f"## {name}", "", "NOT VALID, fix before review:", ""] + [f"- {e}" for e in item] + [""]
        else:
            out += render_line_review(item, folder_years)
    if not entries:
        out += ["No relations files found.", ""]
    return "\n".join(out).rstrip() + "\n"
