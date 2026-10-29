"""Inventory report over a scanned archive. Offline; reads only."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .classify import DOCUMENT_EXTS, classify_document
from .record import is_inside, record_state_and_status
from .scan import Person, scan_archive, unparsed_folders

STATUSES = ("deceased", "living", "unknown")


def check_archive(root: Path, repo_root: Path) -> None:
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"archive path does not exist: {root}")
    if is_inside(root, repo_root):
        raise ValueError(f"archive path is inside the repo: {root}")


def build_report(root: Path, repo_root: Path) -> str:
    check_archive(root, repo_root)
    problems: list[str] = []
    people = scan_archive(root, problems=problems)
    unparsed = unparsed_folders(root)
    info = {p.folder + "\0" + p.line: record_state_and_status(p) for p in people}

    def st(p):
        return info[p.folder + "\0" + p.line][0]

    def rs(p):
        return info[p.folder + "\0" + p.line][1]

    lines = sorted({p.line for p in people})
    out: list[str] = ["# Import Inventory Report", ""]
    out.append(f"People scanned: {len(people)}")
    out.append("")

    out += ["## People by line and status", "", "| Line | deceased | living | unknown | total |", "|---|---|---|---|---|"]
    for ln in lines:
        c = Counter(st(p) for p in people if p.line == ln)
        out.append(f"| {ln} | {c['deceased']} | {c['living']} | {c['unknown']} | {sum(c.values())} |")
    tot = Counter(st(p) for p in people)
    out.append(f"| **all** | {tot['deceased']} | {tot['living']} | {tot['unknown']} | {len(people)} |")
    out.append("")

    shown = [p for p in people if st(p) != "living"]
    n_living = len(people) - len(shown)

    with_id = [p for p in people if len(p.familysearch_ids) == 1]
    no_id = [p for p in people if not p.familysearch_ids]
    multi = [p for p in people if len(p.familysearch_ids) > 1]
    out += [
        "## FamilySearch ids",
        "",
        f"- With exactly one id: {len(with_id)}",
        f"- Without an id: {len(no_id)}",
        f"- With several ids: {len(multi)}",
        "",
    ]
    for p in multi:
        if st(p) != "living":
            out.append(f"- several ids: {p.line} / {p.folder}: {', '.join(p.familysearch_ids)}")
    if any(st(p) != "living" for p in multi):
        out.append("")

    out += ["## Unparsed folders", ""]
    if unparsed:
        out += [f"- {ln} / {fo}" for ln, fo in unparsed]
    else:
        out.append("None.")
    out.append("")

    out += ["## Unreadable folders", ""]
    out += [f"- {x}" for x in sorted(problems)] or ["None."]
    out.append("")

    out += ["## Unknown status (not importable until an ImportRecord says deceased)", ""]
    unk = [p for p in people if st(p) == "unknown"]
    out += [f"- {p.line} / {p.folder}" for p in unk] or ["None."]
    out.append("")

    out += ["## Document actions", ""]
    total_actions: Counter = Counter()
    skip_reasons: Counter = Counter()
    rows: list[str] = []
    for p in people:
        status = st(p)
        c: Counter = Counter()
        for f in p.files:
            if f.suffix.lower() not in DOCUMENT_EXTS:
                continue
            v = classify_document(p, f, status)
            c[v.action] += 1
            if v.action == "skip":
                skip_reasons[v.reason] += 1
        total_actions.update(c)
        if status != "living" and c:
            rows.append(
                f"| {p.line} / {p.folder} | {c['include']} | {c['cite-only']} | {c['skip']} |"
            )
    out.append(
        f"Totals: include {total_actions['include']}, cite-only {total_actions['cite-only']}, "
        f"skip {total_actions['skip']}"
    )
    out.append("")
    if skip_reasons:
        out += ["Skip reasons:", ""]
        out += [f"- {r}: {n}" for r, n in sorted(skip_reasons.items())]
        out.append("")
    if rows:
        out += ["| Person | include | cite-only | skip |", "|---|---|---|---|", *rows, ""]
    out.append(f"Living people: {n_living} (documents counted above, never listed or attached).")
    out.append("")

    out += ["## People needing an ImportRecord", ""]
    need = [p for p in shown if rs(p) in ("missing", "draft", "invalid")]
    out += [f"- {p.line} / {p.folder} ({rs(p)})" for p in need] or ["None."]
    out.append("")
    return "\n".join(out)
