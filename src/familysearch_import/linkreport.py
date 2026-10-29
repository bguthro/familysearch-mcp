"""Link report: verify mined ids and list search candidates. No network here."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace

from .linking import FsPerson, LinkResult, differences, resolve_links
from .record import record_state_and_status
from .scan import IdCandidate, Person, mine_candidates

VERDICTS = ("confirmed", "probable", "conflict", "unlinked", "linked-differs", "needs-search")
_TITLES = {
    "confirmed": "Confirmed (name and dates agree)",
    "probable": "Probable (a year differs by 1-2, or no years to compare)",
    "conflict": "Conflict (two different confirmed ids; nothing chosen)",
    "unlinked": "Unverified (lookup failed; not treated as missing)",
    "linked-differs": "Linked by you; FamilySearch record differs (please review)",
    "needs-search": "Needs search (no usable id found)",
}


def split_by_status(people: Sequence[Person]) -> tuple[list[Person], Counter]:
    """Deceased people plus counts of every status (living/unknown are only counted)."""
    counts: Counter = Counter()
    deceased: list[Person] = []
    for p in people:
        status = record_state_and_status(p)[0]
        counts[status] += 1
        if status == "deceased":
            deceased.append(p)
    return deceased, counts


def build_link_results(
    people: Sequence[Person],
    lookup: Callable[[str], FsPerson],
    search: Callable[[Person], list[FsPerson]],
) -> list[LinkResult]:
    """Deceased people only; living and unknown are never looked up or searched."""
    deceased, _ = split_by_status(people)
    cache: dict[str, FsPerson] = {}
    results: list[LinkResult] = []
    for p in deceased:
        ids = {c.id for c in mine_candidates(p)}
        for pid in sorted(ids):
            if pid not in cache:
                cache[pid] = lookup(pid)
        r = resolve_links(p, {i: cache[i] for i in ids})
        if r.verdict in ("needs-search", "linked-differs"):
            try:
                known = set()
                for cand, f, _ in r.candidates:
                    known |= {cand.id, f.id, f.survivor_id}
                known.discard(None)
                found = [s for s in search(p) if s.id not in known and s.survivor_id not in known]
                r = replace(r, search=tuple(found))
            except Exception:  # a failed search is reported, never read as "no match"
                r = replace(r, search_error=True)
        results.append(r)
    return results


def _span(fs: FsPerson) -> str:
    b = fs.birth_year if fs.birth_year is not None else "?"
    d = fs.death_year if fs.death_year is not None else "?"
    return f"{b}-{d}"


def _fs_text(fs: FsPerson) -> str:
    return f"{fs.name or '(no name)'} {_span(fs)}"


def _where(r: LinkResult, c: IdCandidate) -> str:
    return f"{c.source} ({c.kind})"


def render_link_report(results: Sequence[LinkResult], counts: Counter | dict) -> str:
    by: dict[str, list[LinkResult]] = {v: [] for v in VERDICTS}
    for r in results:
        by[r.verdict].append(r)
    out = ["# Link Report", "", "Read-only verification against FamilySearch. Nothing here is auto-accepted.", ""]
    out += ["## Totals", ""]
    out += [f"- {v}: {len(by[v])}" for v in VERDICTS]
    out += [
        f"- living (not looked up, not listed): {counts.get('living', 0)}",
        f"- unknown status (not looked up, not listed): {counts.get('unknown', 0)}",
        "",
    ]
    for v in VERDICTS:
        if not by[v]:
            continue
        out += [f"## {_TITLES[v]} ({len(by[v])})", ""]
        for r in sorted(by[v], key=lambda r: (r.person.line, r.person.folder)):
            head = f"- {r.person.line} / {r.person.folder}"
            if r.chosen_id:
                head += f" -> {r.chosen_id}"
            out.append(head)
            seen: set[tuple[str, str, str]] = set()
            for cand, fs, outcome in r.candidates:
                key = (cand.id, cand.source, cand.kind)
                if key in seen:
                    continue
                seen.add(key)
                label = outcome
                if cand.kind == "webloc" and outcome == "mismatch":
                    label = "linked by you, FamilySearch record differs"
                elif outcome == "not-found":
                    label = "not a person id on FamilySearch (may be a record id, or deleted)"
                note = f"{cand.id}: {label}; found in {_where(r, cand)}"
                if fs.status in ("ok", "redirected"):
                    note += f"; FamilySearch: {_fs_text(fs)}"
                if fs.status == "redirected":
                    note += f"; merged into {fs.survivor_id or 'unknown'}"
                out.append(f"  - {note}")
                if fs.status in ("ok", "redirected"):
                    out += [f"    - {d}" for d in differences(r.person, fs)]
            if v in ("needs-search", "linked-differs"):
                if r.search_error:
                    out.append("  - search failed (not the same as no match)")
                elif not r.search:
                    out.append("  - search found no candidates")
                for fs in r.search:
                    out.append(f"  - search candidate {fs.id}: {_fs_text(fs)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def links_json(results: Sequence[LinkResult]) -> dict:
    people = []
    for r in results:
        people.append({
            "line": r.person.line,
            "folder": r.person.folder,
            "verdict": r.verdict,
            "chosen_id": r.chosen_id,
            "candidates": [
                {
                    "id": c.id,
                    "source": c.source,
                    "kind": c.kind,
                    "outcome": outcome,
                    "status": fs.status,
                    "survivor_id": fs.survivor_id,
                    "fs_name": fs.name,
                    "birth_year": fs.birth_year,
                    "death_year": fs.death_year,
                }
                for c, fs, outcome in r.candidates
            ],
            "search": [
                {"id": s.id, "name": s.name, "birth_year": s.birth_year, "death_year": s.death_year}
                for s in r.search
            ],
            "search_error": r.search_error,
        })
    summary = Counter(r.verdict for r in results)
    return {"summary": {v: summary.get(v, 0) for v in VERDICTS}, "people": people}
