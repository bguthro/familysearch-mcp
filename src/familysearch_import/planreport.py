"""Plan inputs and outputs: links.json / approvals.json / ledger readers, report, JSON."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .planner import Plan

NON_NAMED_NOTES = ("living", "unknown status")
SHARED_PREFIX = "same file already planned for "


def load_links(path: Path) -> dict[str, str | None]:
    """'<line>/<folder>' -> FS id for confirmed verdicts only (others map to None)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, str | None] = {}
    for p in data.get("people", []):
        key = f"{p['line']}/{p['folder']}"
        out[key] = p.get("chosen_id") if p.get("verdict") == "confirmed" else None
    return out


def load_approvals(path: Path) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"approvals file is not valid JSON: {path}") from e
    links = data.get("approved_links") if isinstance(data, dict) else None
    if not isinstance(links, dict) or not all(
        isinstance(k, str) and "/" in k and isinstance(v, str) and v for k, v in links.items()
    ):
        raise ValueError(f'approvals file must be {{"approved_links": {{"<line>/<folder>": "<FS id>"}}}}: {path}')
    force = data.get("force_upload", [])
    if not isinstance(force, list) or not all(isinstance(f, str) and f.count("/") >= 2 for f in force):
        raise ValueError(f'"force_upload" must be a list of "<line>/<folder>/<filename>" strings: {path}')
    return dict(links)


def load_nofolder_approvals(path: Path) -> dict[str, str]:
    """approved_nofolder: {diagram label: FamilySearch id} for people with no archive folder."""
    path = Path(path)
    if not path.exists():
        return {}
    load_approvals(path)  # validates the rest of the file
    data = json.loads(path.read_text(encoding="utf-8")).get("approved_nofolder", {})
    if not isinstance(data, dict) or not all(isinstance(k, str) and k and isinstance(v, str) and v
                                             for k, v in data.items()):
        raise ValueError(f'"approved_nofolder" must be {{"<label>": "<FS id>"}}: {path}')
    return dict(data)


def load_force_upload(path: Path) -> frozenset[str]:
    path = Path(path)
    if not path.exists():
        return frozenset()
    load_approvals(path)  # validates shape
    return frozenset(json.loads(path.read_text(encoding="utf-8")).get("force_upload", []))


def load_ledger_keys(path: Path, env: str | None = None) -> frozenset[str]:
    """Keys of executor results with status created in this env (env None: any env)."""
    path = Path(path)
    if not path.exists():
        return frozenset()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (isinstance(rec, dict) and isinstance(rec.get("key"), str)
                and rec.get("type") == "result" and rec.get("status") == "created"
                and (env is None or rec.get("env") == env)):
            keys.add(rec["key"])
    return frozenset(keys)


def plan_json(plan: Plan) -> dict:
    return {
        "plan_hash": plan.plan_hash,
        "env": plan.env,
        "people": [asdict(p) for p in plan.people],
    }


def _named(p) -> bool:
    return p.note not in NON_NAMED_NOTES


def render_plan_report(plan: Plan) -> str:
    kinds: Counter = Counter()
    reasons: Counter = Counter()
    for p in plan.people:
        for a in p.actions:
            kinds[(a.state, a.kind)] += 1
            if a.state == "skipped":
                reasons[a.reason] += 1
    person_status = Counter(p.status for p in plan.people)
    person_notes = Counter(p.note for p in plan.people if p.status == "skipped" and p.note)

    lines = [f"# Import plan (dry run)", "", f"Plan hash: `{plan.plan_hash}`", "",
             "Nothing has been written to FamilySearch.", "", "## Totals", ""]
    lines.append(f"- people: {len(plan.people)} ("
                 + ", ".join(f"{n} {s}" for s, n in sorted(person_status.items())) + ")")
    for state in ("planned", "likely-covered", "already-present", "skipped"):
        parts = [f"{n} {k}" for (s, k), n in sorted(kinds.items()) if s == state]
        lines.append(f"- {state}: {sum(n for (s, _), n in kinds.items() if s == state)}"
                     + (f" ({', '.join(parts)})" if parts else ""))
    if person_notes:
        lines += ["", "## People skipped, by reason", ""]
        lines += [f"- {r}: {n}" for r, n in sorted(person_notes.items())]
    if reasons:
        lines += ["", "## Skipped files, by reason", ""]
        lines += [f"- {r}: {n}" for r, n in sorted(reasons.items())]

    shared = [(p, a) for p in plan.people for a in p.actions
              if a.state == "skipped" and a.reason.startswith(SHARED_PREFIX)]
    if shared:
        lines += ["", "## Skipped: identical file shared with another person (attach manually)", ""]
        lines += [f"- {p.line} / {p.folder}: {a.file} ({a.reason})" for p, a in shared]

    lines += ["", "## Deceased people", ""]
    shown = False
    for p in plan.people:
        if not _named(p) or not (p.actions or p.status != "skipped" or p.note):
            continue
        shown = True
        lines.append(f"### {p.line} / {p.folder}")
        lines.append(f"- FamilySearch id: {p.fs_id or 'none'}; status: {p.status}"
                     + (f"; note: {p.note}" if p.note else ""))
        for a in p.actions:
            what = {"upload-document": "upload", "add-source": "cite", "skip": "skip"}[a.kind]
            detail = f" as \"{a.title}\"" if a.title else ""
            lines.append(f"  - {a.state}: {what} {a.file}{detail} ({a.reason})")
        lines.append("")
    if not shown:
        lines += ["(none)", ""]
    return "\n".join(lines).rstrip() + "\n"
