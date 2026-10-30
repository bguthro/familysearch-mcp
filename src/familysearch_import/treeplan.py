"""Tree plan: approved couples, parent links and new deceased people. Pure function of files; no network.

Reads the relationships.json report data, Ben's approvals and links.json. Writes nothing to
FamilySearch. See docs/superpowers/specs/2026-10-30-import-tree-writes-design.md.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Kind = Literal["add-couple", "add-child-parents", "create-person"]
State = Literal["planned", "skipped", "manual"]

NO_PARENTS_NOTE = "child has no parents on FamilySearch"
LIVING_CUTOFF = 1900


@dataclass(frozen=True)
class TreeAction:
    kind: Kind
    key: str
    params: Mapping[str, Any]
    state: State
    reason: str


@dataclass(frozen=True)
class TreePlan:
    env: str
    actions: tuple[TreeAction, ...]
    plan_hash: str


def compute_tree_plan_hash(actions: Sequence[TreeAction], env: str) -> str:
    planned = sorted((asdict(a) for a in actions if a.state == "planned"), key=lambda d: d["key"])
    blob = json.dumps({"env": env, "actions": planned}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_tree_approvals(path: Path) -> dict:
    """approved_relationships / approved_creations / approved_links from approvals.json (missing file: empty)."""
    path = Path(path)
    if not path.exists():
        return {"approved_relationships": [], "approved_creations": [], "approved_links": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"approvals file is not valid JSON: {path}") from e
    if not isinstance(data, dict):
        raise ValueError(f"approvals file must be a JSON object: {path}")
    out: dict = {}
    for name in ("approved_relationships", "approved_creations"):
        v = data.get(name, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f'"{name}" must be a list of strings: {path}')
        out[name] = list(v)
    links = data.get("approved_links", {})
    out["approved_links"] = dict(links) if isinstance(links, dict) else {}
    return out


def _canonical_key(kind: str, a: str, b: str) -> str:
    if kind == "spouse":
        a, b = sorted((a, b))
    return f"{kind}:{a}:{b}"


def _linked_ids(links: Mapping[str, str | None], approvals: Mapping) -> frozenset[str]:
    ids = {v for v in links.values() if v}
    ids |= {v for v in (approvals.get("approved_links") or {}).values() if v}
    return frozenset(ids)


def _label(e: Mapping, side: str) -> str:
    return str(e.get(side) or "")


def build_tree_plan(relationships: Mapping, approvals: Mapping, links: Mapping[str, str | None],
                    env: str) -> TreePlan:
    approved_rel = set(approvals.get("approved_relationships") or [])
    approved_new = set(approvals.get("approved_creations") or [])
    linked = _linked_ids(links, approvals)
    actions: list[TreeAction] = []

    def link_problem(e: Mapping) -> str | None:
        a, b = e.get("a_fs"), e.get("b_fs")
        if not a or not b:
            return "an end has no FamilySearch id"
        for x in (a, b):
            if x not in linked:
                return "edge id does not match a confirmed link in links.json"
        return None

    spouse_seen: set[str] = set()
    by_child: dict[str, list[Mapping]] = {}
    for e in relationships.get("edges", []):
        kind = e.get("kind")
        if kind not in ("spouse", "parent-child"):
            continue
        a, b = e.get("a_fs") or "", e.get("b_fs") or ""
        ckey = _canonical_key(kind, a, b)
        if e.get("key") not in approved_rel and ckey not in approved_rel:
            continue
        if e.get("status") != "missing-in-familysearch":
            if kind == "spouse" and ckey in spouse_seen:
                continue
            if kind == "spouse":
                spouse_seen.add(ckey)
            actions.append(TreeAction(
                "add-couple" if kind == "spouse" else "add-child-parents", ckey,
                {"a": a, "b": b}, "skipped", f"edge status is {e.get('status')}, not missing-in-familysearch"))
            continue
        if kind == "spouse":
            if ckey in spouse_seen:
                continue
            spouse_seen.add(ckey)
            problem = link_problem(e)
            x, y = sorted((a, b))
            names = {x: _label(e, "a") if a == x else _label(e, "b"),
                     y: _label(e, "a") if a == y else _label(e, "b")}
            params = {"a": x, "b": y, "a_label": names[x], "b_label": names[y]}
            if problem:
                actions.append(TreeAction("add-couple", ckey, params, "skipped", problem))
            else:
                actions.append(TreeAction("add-couple", ckey, params, "planned",
                                          "no couple relationship on FamilySearch; approved"))
        else:
            by_child.setdefault(b, []).append(e)

    for child in sorted(by_child):
        edges = by_child[child]
        parents: list[str] = []
        labels: dict[str, str] = {}
        for e in edges:
            if e["a_fs"] not in parents:
                parents.append(e["a_fs"])
            labels[e["a_fs"]] = _label(e, "a")
        parents.sort()
        params = {"child": child, "child_label": _label(edges[0], "b"), "parents": parents,
                  "parent_labels": [labels[p] for p in parents]}
        key = f"child-parents:{child}"
        problem = next((p for p in (link_problem(e) for e in edges) if p), None)
        if problem:
            actions.append(TreeAction("add-child-parents", key, params, "skipped", problem))
        elif len(parents) > 2:
            actions.append(TreeAction("add-child-parents", key, params, "manual",
                                      "more than two parents approved; do this on the website"))
        elif not all(NO_PARENTS_NOTE in (e.get("note") or "") for e in edges):
            actions.append(TreeAction("add-child-parents", key, params, "manual",
                                      "child already has a parent relationship on FamilySearch; "
                                      "manual on the website"))
        else:
            actions.append(TreeAction("add-child-parents", key, params, "planned",
                                      "child has no parents on FamilySearch; approved"))

    for c in relationships.get("create_candidates", []):
        label = c.get("label")
        if label not in approved_new:
            continue
        birth, death = c.get("birth"), c.get("death")
        params = {"name": label, "birth": birth, "death": death, "gender": "Unknown"}
        key = "create-person:" + hashlib.sha256(str(label).encode("utf-8")).hexdigest()[:16]
        if birth is None and death is None:
            actions.append(TreeAction("create-person", key, params, "skipped", "no birth or death year"))
        elif death is None and birth is not None and birth >= LIVING_CUTOFF:
            actions.append(TreeAction("create-person", key, params, "skipped",
                                      "born 1900 or later with no death year (living rule)"))
        else:
            actions.append(TreeAction("create-person", key, params, "planned",
                                      "approved creation; search before create at execution"))

    actions.sort(key=lambda a: (a.kind, a.key))
    return TreePlan(env, tuple(actions), compute_tree_plan_hash(actions, env))


def tree_plan_json(plan: TreePlan) -> dict:
    return {"env": plan.env, "plan_hash": plan.plan_hash, "actions": [asdict(a) for a in plan.actions]}


def render_tree_plan_report(plan: TreePlan) -> str:
    out = [f"# Tree plan {plan.plan_hash[:8]}", "",
           f"Environment: {plan.env}. Plan hash: {plan.plan_hash}", ""]
    for state, title in (("planned", "Planned"), ("manual", "Manual on the website"), ("skipped", "Skipped")):
        rows = [a for a in plan.actions if a.state == state]
        out.append(f"## {title} ({len(rows)})")
        out.append("")
        for a in rows:
            p = a.params
            if a.kind == "add-couple":
                what = f"couple {p.get('a_label') or ''} ({p['a']}) + {p.get('b_label') or ''} ({p['b']})"
            elif a.kind == "add-child-parents":
                parents = ", ".join(f"{n} ({i})" for n, i in zip(p.get("parent_labels", []), p["parents"]))
                what = f"child {p.get('child_label') or ''} ({p['child']}) with parents {parents}"
            else:
                what = f"new person {p['name']} (b. {p.get('birth')}, d. {p.get('death')}, gender {p['gender']})"
            out.append(f"- {a.kind}: {what} - {a.reason}")
        out.append("")
    return "\n".join(out)


def write_tree_plan(archive: Path, plan: TreePlan) -> Path:
    import_dir = Path(archive) / "_import"
    import_dir.mkdir(parents=True, exist_ok=True)
    tag = plan.plan_hash[:8]
    path = import_dir / f"TreePlan.{tag}.json"
    path.write_text(json.dumps(tree_plan_json(plan), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (import_dir / f"TreePlanReport.{tag}.md").write_text(render_tree_plan_report(plan), encoding="utf-8")
    return path
