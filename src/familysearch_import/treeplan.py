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
    warnings: tuple[str, ...] = ()  # report only; not part of the hash


KIND_RANK = {"add-couple": 0, "add-child-parents": 1, "create-person": 2}


def creation_key(label: str, birth: int | None, death: int | None) -> str:
    """Action key of a create-person: label AND years, so same-named people never share a key."""
    blob = json.dumps([label, birth, death], ensure_ascii=False, separators=(",", ":"))
    return "create-person:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
    if not isinstance(links, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in links.items()):
        raise ValueError(f'"approved_links" must map "<line>/<folder>" to a FamilySearch id: {path}')
    out["approved_links"] = dict(links)
    return out


def _canonical_key(kind: str, a: str, b: str) -> str:
    if kind == "spouse":
        a, b = sorted((a, b))
    return f"{kind}:{a}:{b}"


def _linked_ids(links: Mapping[str, str | None], approvals: Mapping) -> frozenset[str]:
    """Every id currently linked to a folder (the planner's current_link rule, per folder)."""
    from .planner import current_link
    approved = approvals.get("approved_links") or {}
    return frozenset(i for i in (current_link(k, approved, links) for k in set(links) | set(approved)) if i)


def _label(e: Mapping, side: str) -> str:
    return str(e.get(side) or "")


def build_tree_plan(relationships: Mapping, approvals: Mapping, links: Mapping[str, str | None],
                    env: str) -> TreePlan:
    from .planner import current_link
    built_for = relationships.get("env")
    if built_for is None:
        raise ValueError("relationships.json does not record its environment (older file); "
                         "re-run the relationships command with --env")
    if built_for != env:
        raise ValueError(f"relationships.json was built against {built_for}, but --env is {env}")
    approved_rel = set(approvals.get("approved_relationships") or [])
    approved_new = set(approvals.get("approved_creations") or [])
    approved_links = approvals.get("approved_links") or {}
    actions: list[TreeAction] = []
    matched_rel: set[str] = set()
    matched_new: set[str] = set()

    def link_problem(pairs: Sequence[tuple[str, str | None]]) -> str | None:
        """pairs: (FamilySearch id, folder key) per end; each must equal that folder's CURRENT link."""
        for fs, folder in pairs:
            if not fs:
                return "an end has no FamilySearch id"
            if not folder:
                return "an end has no folder key in relationships.json; re-run the relationships command"
            if current_link(folder, approved_links, links) != fs:
                return "an end's id does not match its folder's current link (links.json / approved_links)"
        return None

    spouse_seen: set[str] = set()
    by_child: dict[str, list[Mapping]] = {}
    all_parent_edges: dict[str, list[Mapping]] = {}
    for e in relationships.get("edges", []):
        if e.get("kind") == "parent-child" and e.get("b_fs"):
            all_parent_edges.setdefault(e["b_fs"], []).append(e)
        kind = e.get("kind")
        if kind not in ("spouse", "parent-child"):
            continue
        a, b = e.get("a_fs") or "", e.get("b_fs") or ""
        ckey = _canonical_key(kind, a, b)
        if e.get("key") not in approved_rel and ckey not in approved_rel:
            continue
        matched_rel.update(k for k in (e.get("key"), ckey) if k in approved_rel)
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
            problem = link_problem([(a, e.get("a_folder")), (b, e.get("b_folder"))])
            ends = sorted([(a, _label(e, "a"), e.get("a_folder")), (b, _label(e, "b"), e.get("b_folder"))])
            params = {"a": ends[0][0], "b": ends[1][0], "a_label": ends[0][1], "b_label": ends[1][1],
                      "a_folder": ends[0][2], "b_folder": ends[1][2]}
            if problem:
                actions.append(TreeAction("add-couple", ckey, params, "skipped", problem))
            else:
                actions.append(TreeAction("add-couple", ckey, params, "planned",
                                          "no couple relationship on FamilySearch; approved"))
        else:
            by_child.setdefault(b, []).append(e)

    warnings: list[str] = []
    for child in sorted(by_child):
        edges = by_child[child]
        pinfo: dict[str, tuple[str, str | None]] = {}
        for e in edges:
            pinfo.setdefault(e["a_fs"], (_label(e, "a"), e.get("a_folder")))
        parents = sorted(pinfo)
        params = {"child": child, "child_label": _label(edges[0], "b"), "child_folder": edges[0].get("b_folder"),
                  "parents": parents, "parent_labels": [pinfo[p][0] for p in parents],
                  "parent_folders": [pinfo[p][1] for p in parents]}
        key = f"child-parents:{child}"
        problem = link_problem([(child, edges[0].get("b_folder"))] + [(p, pinfo[p][1]) for p in parents])
        approved_parents = {e["a_fs"] for e in edges}
        others = sorted({e["a_fs"] for e in all_parent_edges.get(child, []) if e["a_fs"] not in approved_parents
                         and e.get("status") != "present"})
        if others:
            warnings.append(f"child {child} also has archive parent(s) {', '.join(others)} that are not "
                            "approved; adding only some parents now makes the rest manual on the website")
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

    cands = list(relationships.get("create_candidates", []))
    label_counts: dict[str, int] = {}
    for c in cands:
        label_counts[c.get("label")] = label_counts.get(c.get("label"), 0) + 1
    seen_keys: set[str] = set()
    for c in cands:
        label = c.get("label")
        birth, death = c.get("birth"), c.get("death")
        key = creation_key(str(label), birth, death)
        by_label, by_key = label in approved_new, key in approved_new
        if not (by_label or by_key):
            continue
        matched_new.update(k for k in (label, key) if k in approved_new)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        params = {"name": label, "birth": birth, "death": death, "gender": "Unknown"}
        if birth is None and death is None:
            actions.append(TreeAction("create-person", key, params, "skipped", "no birth or death year"))
        elif death is None and birth is not None and birth >= LIVING_CUTOFF:
            actions.append(TreeAction("create-person", key, params, "skipped",
                                      "born 1900 or later with no death year (living rule)"))
        elif by_label and not by_key and label_counts[label] > 1:
            actions.append(TreeAction("create-person", key, params, "manual",
                                      f"{label_counts[label]} candidates share this name; approve this one "
                                      f"by its key {key} in approved_creations"))
        else:
            actions.append(TreeAction("create-person", key, params, "planned",
                                      "approved creation; search before create at execution"))

    warnings = [f"approved relationship matches no edge: {k}" for k in sorted(approved_rel - matched_rel)] + \
        [f"approved creation matches no candidate: {k}" for k in sorted(approved_new - matched_new)] + warnings
    actions.sort(key=lambda a: (KIND_RANK[a.kind], a.key))
    return TreePlan(env, tuple(actions), compute_tree_plan_hash(actions, env), tuple(warnings))


def tree_plan_json(plan: TreePlan) -> dict:
    return {"env": plan.env, "plan_hash": plan.plan_hash, "actions": [asdict(a) for a in plan.actions]}


def render_tree_plan_report(plan: TreePlan) -> str:
    out = [f"# Tree plan {plan.plan_hash[:8]}", "",
           f"Environment: {plan.env}. Plan hash: {plan.plan_hash}", ""]
    if plan.warnings:
        out += [f"## Warnings ({len(plan.warnings)})", ""] + [f"- {w}" for w in plan.warnings] + [""]
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
            out.append(f"- {a.kind}: {what} - {a.reason}" + (f" [key {a.key}]" if a.kind == "create-person" else ""))
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
