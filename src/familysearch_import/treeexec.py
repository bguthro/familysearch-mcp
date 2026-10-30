"""Tree executor: writes approved couples, parent links and new deceased people to FamilySearch.

Mirrors execute.py's safety model (dry run by default, plan hash + approval, run lock, ledger with
intent/progress/result, fresh pre-checks, read-back, rollback by recorded ids only). Every gate runs
before network I/O and before any ledger line.

Tree runs use their own ledger file (``tree-ledger.jsonl``) so upload reconciliation never sees tree
intents (and vice versa); the run lock ``execute.lock`` is shared, so the two never run together.

Proof model: before each write the executor snapshots the (relationship, id) set of every involved
person plus up to two unrelated, deceased, linked control people. After the write the snapshots are
taken again and the difference must equal EXACTLY the expected change. The new relationship id is also
read by id and must name exactly the planned people. A two-parent child link is only runnable when the
parents are ALREADY a couple (so FamilySearch derives no new couple); the read-back asserts that no new
couple id appeared for them.
"""

from __future__ import annotations

import hmac
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path

from .execute import (
    DEFAULT_MAX_ACTIONS, ENVS, MIN_SETTLE_SECONDS, PRODUCTION_HARD_CAP, REASON, GateError, Ledger,
    Outcome, RollbackSummary, RunSummary, _Archive, _forbid_repo, _http_status, _warn, check_run_gates,
    intent_age_seconds, utc_now, valid_object_id,
)
from .prod_reader import ReadFailed
from .record import is_inside, record_state_and_status
from .scan import Person
from .treeplan import (
    KIND_RANK, LIVING_CUTOFF, TreeAction, _linked_ids, compute_tree_plan_hash, creation_key,
    load_tree_approvals,
)

ROLLBACK_REASON = "Rolled back import from family research archive"
TREE_KINDS = ("add-couple", "add-child-parents", "create-person")
_STATES = ("planned", "skipped", "manual")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{3,4}$")
_ACTION_FIELDS = tuple(f.name for f in fields(TreeAction))
_RESULT_STATUSES = ("created", "skipped", "failed", "failed-readback", "partial", "ambiguous")
LEDGER_NAME = "tree-ledger.jsonl"
GX = "http://gedcomx.org/"
ALLOWED_FIELDS = {"add-couple": ("couple_id",), "add-child-parents": ("child_parents_id",),
                  "create-person": ("person_id",)}
ROLLBACK_ORDER = ("child_parents_id", "couple_id", "person_id")
N_CONTROLS = 2


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class TreePlanFile:
    plan_hash: str
    env: str
    actions: tuple[TreeAction, ...]
    path: Path
    verified: bool = False  # set only by verify_tree_plan_for_execution

    def planned(self) -> tuple[TreeAction, ...]:
        return tuple(sorted((a for a in self.actions if a.state == "planned"),
                            key=lambda a: (KIND_RANK[a.kind], a.key)))


def _norm(s: str | None) -> str:
    from .planner import _norm as n
    return n(s)


def _is_year(v) -> bool:
    return v is None or (isinstance(v, int) and not isinstance(v, bool) and 0 < v < 3000)


def _is_id(v) -> bool:
    return isinstance(v, str) and bool(_ID_RE.match(v))


def _is_folder(v) -> bool:
    if not isinstance(v, str) or v.count("/") != 1:
        return False
    return all(p and p not in (".", "..") and "\\" not in p and "\0" not in p for p in v.split("/"))


def _deceased_by_rule(birth, death) -> bool:
    if death is not None:
        return True
    return birth is not None and birth < LIVING_CUTOFF


def _validate_runnable(a: TreeAction) -> None:
    """Structural checks for a planned action; re-derives the key from the params."""
    tag = a.key[:12]
    p = a.params
    if not isinstance(p, dict):
        raise GateError(f"planned action {tag} has malformed params")
    if a.kind == "add-couple":
        x, y = p.get("a"), p.get("b")
        if not (_is_id(x) and _is_id(y)) or x == y or x > y:
            raise GateError(f"planned action {tag} has malformed couple ids")
        if not (_is_folder(p.get("a_folder")) and _is_folder(p.get("b_folder"))):
            raise GateError(f"planned action {tag} has malformed folder keys")
        if a.key != f"spouse:{x}:{y}":
            raise GateError(f"planned action {tag} key does not match its content")
    elif a.kind == "add-child-parents":
        child, parents = p.get("child"), p.get("parents")
        if not _is_id(child) or not isinstance(parents, list) or not 1 <= len(parents) <= 2 \
                or not all(_is_id(x) for x in parents) or len(set(parents)) != len(parents) \
                or child in parents:
            raise GateError(f"planned action {tag} has malformed child/parent ids")
        folders = p.get("parent_folders")
        if not _is_folder(p.get("child_folder")) or not isinstance(folders, list) \
                or len(folders) != len(parents) or not all(_is_folder(f) for f in folders):
            raise GateError(f"planned action {tag} has malformed folder keys")
        if a.key != f"child-parents:{child}":
            raise GateError(f"planned action {tag} key does not match its content")
    elif a.kind == "create-person":
        name = p.get("name")
        if not isinstance(name, str) or not name.strip():
            raise GateError(f"planned action {tag} has no name")
        if not (_is_year(p.get("birth")) and _is_year(p.get("death"))):
            raise GateError(f"planned action {tag} has a malformed year")
        if p.get("gender") != "Unknown":
            raise GateError(f"planned action {tag} gender must be Unknown")
        if not _deceased_by_rule(p.get("birth"), p.get("death")):
            raise GateError(f"planned action {tag} person is not deceased by the archive rule")
        if a.key != creation_key(name, p.get("birth"), p.get("death")):
            raise GateError(f"planned action {tag} key does not match its content")
    else:
        raise GateError(f"planned action {tag} has non-runnable kind {a.kind!r}")


def _still_approved(a: TreeAction, approvals: dict) -> bool:
    rel, new = set(approvals["approved_relationships"]), set(approvals["approved_creations"])
    p = a.params
    if a.kind == "add-couple":
        return a.key in rel
    if a.kind == "add-child-parents":
        return all(f"parent-child:{par}:{p['child']}" in rel for par in p["parents"])
    return a.key in new or p["name"] in new


def verify_tree_plan_for_execution(archive: Path, plan_path: Path, confirm_hash: str, env: str) -> TreePlanFile:
    """Every plan-level gate. Raises GateError; no network I/O, writes nothing."""
    archive, plan_path = Path(archive), Path(plan_path)
    _forbid_repo(archive, "archive")
    _forbid_repo(plan_path, "plan")
    import_dir = archive / "_import"
    if not is_inside(plan_path, import_dir) or plan_path.resolve() == import_dir.resolve():
        raise GateError(f"plan must be a file inside {import_dir}")
    if not isinstance(confirm_hash, str) or not _HASH_RE.match(confirm_hash):
        raise GateError("--confirm-hash must be a 64-character lowercase hex plan hash")
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise GateError(f"cannot read plan {plan_path.name}: {type(e).__name__}") from e
    if not isinstance(data, dict) or not isinstance(data.get("plan_hash"), str) \
            or not isinstance(data.get("actions"), list):
        raise GateError("tree plan is malformed")
    if env not in ENVS:
        raise GateError(f"--env must be beta or production, got {env!r}")
    plan_env = data.get("env")
    if plan_env not in ENVS:
        raise GateError("tree plan has no environment; re-plan")
    if plan_env != env:
        raise GateError(f"plan was built against {plan_env}, but --env is {env}")

    actions = []
    for raw in data["actions"]:
        if not isinstance(raw, dict) or set(raw) != set(_ACTION_FIELDS):
            raise GateError("tree plan has an action with unexpected fields")
        if raw["kind"] not in TREE_KINDS or raw["state"] not in _STATES \
                or not isinstance(raw["key"], str) or not isinstance(raw["reason"], str) \
                or not isinstance(raw["params"], dict):
            raise GateError("tree plan has a malformed action")
        actions.append(TreeAction(raw["kind"], raw["key"], raw["params"], raw["state"], raw["reason"]))
    recomputed = compute_tree_plan_hash(actions, plan_env)

    approvals_path = import_dir / "approvals.json"
    if not approvals_path.is_file():
        raise GateError("approvals.json not found; the tree plan has not been approved")
    try:
        approvals = load_tree_approvals(approvals_path)  # validates shape
        approved = json.loads(approvals_path.read_text(encoding="utf-8")).get("approved_tree_plan_hash")
    except (OSError, ValueError) as e:
        raise GateError(f"approvals.json is unreadable or malformed: {type(e).__name__}") from e
    if not isinstance(approved, str) or not _HASH_RE.match(approved):
        raise GateError("approvals.json has no valid approved_tree_plan_hash")

    for label, value in (("stored plan_hash", data["plan_hash"]), ("recomputed hash", recomputed),
                         ("approved_tree_plan_hash", approved)):
        if not hmac.compare_digest(value.encode(), confirm_hash.encode()):
            raise GateError(f"--confirm-hash does not match the {label}")
    for a in actions:
        if a.state == "planned":
            _validate_runnable(a)
            if not _still_approved(a, approvals):
                raise GateError(f"planned action {a.key[:40]} is no longer in approvals.json; re-plan")
    return TreePlanFile(data["plan_hash"], plan_env, tuple(actions), plan_path, verified=True)


# ---------------------------------------------------------------------- run gates


def _check_only(only: Sequence[str]) -> frozenset[str]:
    out = set()
    for o in only:
        if not isinstance(o, str) or not o or o != o.strip() or any(c.isspace() or ord(c) < 32 for c in o):
            raise GateError(f"--only must be an action key, got {o!r}")
        out.add(o)
    return frozenset(out)


def check_tree_run_gates(env: str, allow_production: bool, only: Sequence[str], max_actions: int) -> frozenset[str]:
    """Same gates as uploads (production: allow flag, a scope, cap 25); scope is action keys."""
    scope = _check_only(only)
    # execute.check_run_gates validates LINE/FOLDER scopes; hand it a placeholder when a scope exists.
    check_run_gates(env, allow_production, ["tree/scope"] if scope else [], max_actions)
    return scope


# --------------------------------------------------------------------- services


@dataclass
class TreeServices:
    """reader: get_fs_person(id), get_relatives(id, strict), search_duplicates(given, surname, birth,
    death), get_couple(id), get_child_and_parents(id), get_couple_ids(a, b), get_source_ids(id),
    get_memory_ids(id), get_change_summary(id)  (see ReadOnlyFS);
    writer: create_couple, create_parent_child, create_person, delete_couple,
    delete_child_and_parents, delete_person (see GuardedTreeWriter / familysearch.tree.TreeWriter)."""
    reader: object
    writer: object
    archive: Path
    env: str


def tree_ledger_path(archive: Path) -> Path:
    return Path(archive) / "_import" / LEDGER_NAME


# ------------------------------------------------------------------- ledger lines


def _intent(ledger: Ledger, a: TreeAction, env: str, plan_hash: str) -> str:
    intent_id = uuid.uuid4().hex
    ledger._append({"type": "intent", "intent_id": intent_id, "ts": utc_now(), "key": a.key,
                    "kind": a.kind, "env": env, "plan_hash": plan_hash, "params": dict(a.params)})
    return intent_id


def _progress(ledger: Ledger, *, key: str, env: str, intent_id: str, created: dict) -> None:
    ledger._append({"type": "progress", "intent_id": intent_id, "ts": utc_now(), "key": key,
                    "env": env, "created": dict(created)})


def _result(ledger: Ledger, *, intent_id: str, key: str, kind: str, env: str, status: str, reason: str,
            created: dict | None = None, params: dict | None = None, reconciled: bool = False) -> None:
    if status not in _RESULT_STATUSES:
        raise ValueError(f"bad result status {status!r}")
    ledger._append({"type": "result", "intent_id": intent_id, "ts": utc_now(), "key": key, "kind": kind,
                    "env": env, "status": status, "reason": reason, "created": dict(created or {}),
                    "params": dict(params or {}), "reconciled": reconciled})


def _progress_for(recs: list[dict], intent_id: str) -> dict:
    """All ids recorded for an intent (progress lines merged in order)."""
    out: dict = {}
    for r in recs:
        if r.get("type") == "progress" and r.get("intent_id") == intent_id and isinstance(r.get("created"), dict):
            out.update(r["created"])
    return out


def _require_guarded_writer(services: TreeServices) -> None:
    if services.env == "production" and not isinstance(services.writer, GuardedTreeWriter):
        raise GateError("production writes require the registry-guarded writer (GuardedTreeWriter)")


def _tree_intents(ledger: Ledger, env: str) -> list[dict]:
    return [r for r in ledger.unresolved_intents(env) if r.get("kind") in TREE_KINDS]


# ----------------------------------------------------------------- pre-checks


class _Stop(Exception):
    """Internal: stop the run (fail closed)."""


class _Skip(Exception):
    """Internal: a fresh pre-check refused this action; no write."""


@dataclass
class _State:
    """Current link state, read once per run."""
    approved_links: dict
    links: dict
    archive: _Archive
    linked: frozenset[str]


def _load_state(archive: Path) -> _State:
    from .planreport import load_links
    imp = Path(archive) / "_import"
    try:
        approvals = load_tree_approvals(imp / "approvals.json")
    except (OSError, ValueError) as e:
        raise GateError(f"approvals.json unreadable: {type(e).__name__}") from e
    if not (imp / "links.json").is_file():
        raise GateError("links.json not found; the link re-check needs it")
    try:
        links = load_links(imp / "links.json")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise GateError(f"links.json unreadable: {type(e).__name__}") from e
    return _State(approvals["approved_links"], links, _Archive(Path(archive)),
                  _linked_ids(links, approvals))


def _ends(a: TreeAction) -> list[tuple[str, str]]:
    """(FamilySearch id, folder key) of every archive person the action touches."""
    p = a.params
    if a.kind == "add-couple":
        return [(p["a"], p["a_folder"]), (p["b"], p["b_folder"])]
    return [(p["child"], p["child_folder"])] + list(zip(p["parents"], p["parent_folders"]))


def _check_archive_side(a: TreeAction, state: _State) -> None:
    """Per person: the folder's CURRENT link equals the planned id, and the archive still says deceased."""
    from .planner import current_link
    for fs_id, folder in _ends(a):
        link = current_link(folder, state.approved_links, state.links)
        if link != fs_id:
            raise _Skip("link revoked or changed since planning; re-plan" if not link
                        else "link changed since planning; re-plan")
        line, _, name = folder.partition("/")
        person = state.archive.person(line, name)
        if person is None:
            raise _Skip("person folder not found in archive")
        status, rec_state = record_state_and_status(person)
        if rec_state == "invalid":
            raise _Skip("ImportRecord.json is unreadable or invalid")
        if status != "deceased":
            raise _Skip("archive status is no longer deceased")


def _check_person(reader, pid: str, what: str) -> None:
    """Readable, ok, same id, deceased (living is False). Raises _Skip. ReadFailed propagates."""
    fs = reader.get_fs_person(pid)
    if fs.status in ("redirected", "not-found", "deleted"):
        raise _Skip(f"{what} on FamilySearch is {fs.status}")
    if fs.status != "ok":
        raise _Stop(f"{what} read error")
    if getattr(fs, "id", None) != pid:
        raise _Skip(f"{what} id no longer matches the plan")
    if fs.living is not False:
        raise _Skip(f"{what} is living or living status unknown on FamilySearch")


def _rels(reader, pid: str, strict: bool = True) -> list[tuple[str, str]]:
    return [(r.relationship, r.fs.id) for r in reader.get_relatives(pid, strict=strict)]


def _snap(reader, ids: Sequence[str]) -> dict[str, frozenset]:
    return {i: frozenset(_rels(reader, i)) for i in ids}


def _pick_controls(reader, linked: frozenset[str], exclude: set[str], before: dict) -> list[str]:
    """Up to N_CONTROLS deceased, linked people unrelated to every subject. Raises _Skip if none."""
    related = {rid for rels in before.values() for _, rid in rels}
    out: list[str] = []
    for cid in sorted(linked - exclude - related)[:8]:
        try:
            _check_person(reader, cid, "control person")
        except _Skip:
            continue
        snap = frozenset(_rels(reader, cid))
        if any(rid in exclude for _, rid in snap):
            continue
        before[cid] = snap
        out.append(cid)
        if len(out) == N_CONTROLS:
            break
    if not out:
        raise _Skip("no unrelated deceased linked person available as a negative control")
    return out


def _search_hits(reader, p: dict):
    """Unfiltered duplicate search for a planned person. Raises _Skip when it cannot be trusted."""
    from .linking import _parse_name
    parsed = _parse_name(p["name"])
    if not parsed.given or not parsed.surname:
        raise _Skip("name lacks a given name and a surname; cannot search for duplicates; create it by hand")
    if p.get("birth") is None and p.get("death") is None:
        raise _Skip("no usable year for the duplicate search")
    alts = [a for a in parsed.alts if a and a != parsed.surname]  # surnames from parenthesised name parts
    try:
        return reader.search_duplicates(parsed.shown_given, parsed.shown_surname, p.get("birth"), p.get("death"),
                                        alt_surnames=alts)
    except Exception as e:
        raise _Skip(f"duplicate search failed ({type(e).__name__}); nothing created") from e


def _hit_text(hits) -> str:
    ids = sorted({h.id for h in hits if h.living is False})
    hidden = len(hits) - len([h for h in hits if h.living is False])
    return ", ".join(ids) + (f" (+{hidden} living or unknown-status)" if hidden else "")


def _precheck(a: TreeAction, services: TreeServices, state: _State,
              assumed_couples: set | None = None) -> dict:
    """Fresh reads right before the write. Returns context for the read-back; raises _Skip/_Stop."""
    r, p = services.reader, a.params
    if a.kind == "create-person":
        if not _deceased_by_rule(p.get("birth"), p.get("death")):
            raise _Skip("person is not deceased by the archive rule")
        if p.get("gender") != "Unknown":
            raise _Skip("gender must be Unknown")
        hits = _search_hits(r, p)
        if hits:
            raise _Skip("plausible match " + _hit_text(hits))
        return {}
    _check_archive_side(a, state)
    if a.kind == "add-couple":
        x, y = p["a"], p["b"]
        subjects = [x, y]
        for pid in subjects:
            _check_person(r, pid, f"person {pid}")
        before = _snap(r, subjects)
        if ("spouse", y) in before[x] or ("spouse", x) in before[y]:
            raise _Skip("a couple relationship between them already exists on FamilySearch")
        if any(rid == y for _, rid in before[x]) or any(rid == x for _, rid in before[y]):
            raise _Skip("they already have a contradictory relationship on FamilySearch")
    else:
        child, parents = p["child"], list(p["parents"])
        subjects = [child, *parents]
        for pid in subjects:
            _check_person(r, pid, f"person {pid}")
        before = _snap(r, subjects)
        if any(rel == "parent" for rel, _ in before[child]):
            raise _Skip("child already has a parent relationship on FamilySearch")
        if any(rid in parents for _, rid in before[child]) or any(rid == child for par in parents
                                                                   for _, rid in before[par]):
            raise _Skip("child already has a contradictory relationship with a parent")
        if len(parents) == 2 and any(rel in ("parent", "child") and rid == parents[1]
                                     for rel, rid in before[parents[0]]):
            raise _Skip("the two parents are parent and child of each other on FamilySearch")
        couple_ids = None
        if len(parents) == 2:
            is_couple = ("spouse", parents[1]) in before[parents[0]] and ("spouse", parents[0]) in before[parents[1]]
            if not is_couple and frozenset(parents) not in (assumed_couples or set()):
                raise _Skip("parents not yet a couple; approve add-couple first")
            if is_couple:
                couple_ids = sorted(r.get_couple_ids(parents[0], parents[1]))
    controls = _pick_controls(r, state.linked, set(subjects), before)
    return {"subjects": subjects, "controls": controls, "before": before,
            "couple_ids": couple_ids if a.kind == "add-child-parents" else None}


# ---------------------------------------------------------------------- writes


def _person_body(p: dict) -> dict:
    facts = []
    for kind, year in (("Birth", p.get("birth")), ("Death", p.get("death"))):
        if year is not None:
            facts.append({"type": GX + kind, "date": {"original": str(year)}})
    body: dict = {"living": False, "gender": {"type": GX + "Unknown"},
                  "names": [{"nameForms": [{"fullText": p["name"]}]}]}
    if facts:
        body["facts"] = facts
    return body


def _do_write(a: TreeAction, services: TreeServices) -> str:
    w, p = services.writer, a.params
    if a.kind == "add-couple":
        return w.create_couple(p["a"], p["b"], reason=REASON)
    if a.kind == "add-child-parents":
        return w.create_parent_child(child_id=p["child"], parent_ids=list(p["parents"]), reason=REASON)
    return w.create_person(_person_body(p), reason=REASON)


def _expected(a: TreeAction, before: dict) -> dict:
    exp = {k: set(v) for k, v in before.items()}
    p = a.params
    if a.kind == "add-couple":
        exp[p["a"]].add(("spouse", p["b"]))
        exp[p["b"]].add(("spouse", p["a"]))
    else:
        for par in p["parents"]:
            exp[p["child"]].add(("parent", par))
            exp[par].add(("child", p["child"]))
    return exp


def _diff(after: dict, exp: dict) -> str | None:
    """The after snapshot must equal the expected one exactly."""
    for pid, want in exp.items():
        if set(after[pid]) != want:
            who = "an unrelated control person" if pid not in after.get("_subjects", ()) else "an involved person"
            return f"read-back: {who} has a relationship change beyond the planned one"
    return None


def _readback(a: TreeAction, new_id: str, ctx: dict, services: TreeServices) -> str | None:
    """None when the new state is proven exactly; else a reason."""
    r, p = services.reader, a.params
    if a.kind == "create-person":
        fs = r.get_fs_person(new_id)
        if fs.status != "ok" or fs.id != new_id:
            return "read-back: the new person is not readable"
        if _norm(fs.name) != _norm(p["name"]):
            return "read-back: the new person's name does not match"
        if fs.living is not False:
            return "read-back: the new person is not marked deceased"
        if (p.get("birth") is not None and fs.birth_year != p["birth"]) \
                or (p.get("death") is not None and fs.death_year != p["death"]):
            return "read-back: the new person's birth/death year does not match"
        if _rels(r, new_id, strict=False):
            return "read-back: the new person already has relationships"
        return None
    before = ctx["before"]
    after = _snap(r, list(before))
    after["_subjects"] = tuple(ctx["subjects"])  # type: ignore[assignment]
    problem = _diff(after, _expected(a, before))
    if problem:
        return problem
    if a.kind == "add-couple":
        if r.get_couple(new_id) != frozenset((p["a"], p["b"])):
            return "read-back: the new relationship id does not name exactly these two people"
        if r.get_couple_ids(p["a"], p["b"]) != [new_id]:  # sets of person ids cannot show a duplicate
            return "read-back: more than one couple relationship exists between them now"
        return None
    if r.get_child_and_parents(new_id) != (p["child"], frozenset(p["parents"])):
        return "read-back: the new relationship id does not name exactly this child and these parents"
    if ctx.get("couple_ids") is not None and sorted(r.get_couple_ids(*p["parents"])) != ctx["couple_ids"]:
        return "read-back: a new couple relationship appeared between the parents"
    return None


def _write_one(a: TreeAction, who: str, ctx: dict, plan_hash: str, services: TreeServices,
               ledger: Ledger, out: list[Outcome]) -> None:
    env = services.env
    intent_id = _intent(ledger, a, env, plan_hash)
    created: dict[str, str] = {}
    finished = False
    common = dict(intent_id=intent_id, key=a.key, kind=a.kind, env=env, params=dict(a.params))

    def fail(status: str, reason: str) -> None:
        _result(ledger, status=status, reason=reason, created=dict(created), **common)
        out.append(Outcome(a.key, a.kind, who, status, reason))
        raise _Stop(reason)

    try:
        try:
            new_id = _do_write(a, services)
        except Exception as e:
            # A person create that raised may still have happened (timeout after the POST): ambiguous.
            fail("ambiguous" if a.kind == "create-person" else "failed", f"write error: {_http_status(e)}")
        if not valid_object_id(new_id):
            fail("failed-readback", "write returned a missing or invalid id")
        created[ALLOWED_FIELDS[a.kind][0]] = new_id
        _progress(ledger, key=a.key, env=env, intent_id=intent_id, created=created)  # crash window
        try:
            problem = _readback(a, new_id, ctx, services)
        except Exception as e:  # incl. expired token: the created id is already on disk
            fail("failed-readback", f"read-back failed: {_http_status(e)}")
        if problem:
            fail("failed-readback", problem)
        _result(ledger, status="created", reason="created", created=created, **common)
        finished = True
        out.append(Outcome(a.key, a.kind, who, "created", "created"))
    except _Stop:
        raise
    except BaseException as e:  # KeyboardInterrupt etc.: keep every id known so far, then re-raise
        if finished:
            raise
        try:
            _result(ledger, status="partial" if created else "ambiguous",
                    reason=f"interrupted: {type(e).__name__}", created=dict(created), **common)
        except Exception:
            pass
        raise


def _who(a: TreeAction) -> str:
    p = a.params
    if a.kind == "add-couple":
        return f"{p.get('a')} + {p.get('b')}"
    if a.kind == "add-child-parents":
        return f"{p.get('child')} <- {','.join(p.get('parents') or [])}"
    return "new person"


def _reconcile(services: TreeServices, ledger: Ledger, execute: bool, out: list[Outcome],
               held: set[str], blocked: dict[str, str]) -> None:
    """Intents with no result: never re-write blindly. With progress ids -> partial (rollback it);
    without -> ambiguous (resolve it by hand after checking FamilySearch). Both block the key."""
    recs = ledger.records()
    for rec in _tree_intents(ledger, services.env):
        key, iid = rec.get("key"), rec.get("intent_id")
        if not (isinstance(key, str) and isinstance(iid, str)):
            raise _Stop("an unresolved ledger intent is malformed; resolve it manually")
        created = _progress_for(recs, iid)
        if any(not valid_object_id(v) for v in created.values()):
            raise _Stop("an unresolved ledger intent has an invalid progress id; resolve manually")
        status = "partial" if created else "ambiguous"
        reason = ("interrupted after the write returned an id" if created
                  else "interrupted; cannot tell whether the write happened")
        if execute:
            _result(ledger, intent_id=iid, key=key, kind=rec["kind"], env=services.env, status=status,
                    reason=reason, created=created, params=rec.get("params") or {}, reconciled=True)
        held.add(key)
        blocked[key] = status
        out.append(Outcome(key, rec["kind"], "", status, reason))


def run_tree_plan(plan: TreePlanFile, services: TreeServices, ledger: Ledger, *, only: Sequence[str] = (),
                  max_actions: int = DEFAULT_MAX_ACTIONS, execute: bool = False,
                  allow_production: bool = False) -> RunSummary:
    scope = check_tree_run_gates(services.env, allow_production, only, max_actions)
    if not isinstance(plan, TreePlanFile) or not plan.verified:
        raise GateError("plan was not verified by verify_tree_plan_for_execution")
    if execute and not (ledger.path.parent / "execute.lock").is_file():
        raise GateError("execute requires the run lock to be held")
    if plan.env != services.env:
        raise GateError(f"plan was built against {plan.env!r}, services are {services.env!r}")
    planned = plan.planned()
    for a in planned:
        _validate_runnable(a)
    unknown = scope - {a.key for a in planned}
    if unknown:
        raise GateError(f"--only key is not a planned action: {sorted(unknown)[0][:40]}")
    if execute:
        _require_guarded_writer(services)
    state = _load_state(services.archive)

    out: list[Outcome] = []
    summary = RunSummary(execute, out)
    executed = set(ledger.executed_keys(services.env))
    blocked = _tree_blocked(ledger, services.env)
    held: set[str] = set()
    assumed: set = set()
    writes = 0

    def skip(a: TreeAction, reason: str) -> None:
        out.append(Outcome(a.key, a.kind, _who(a), "skipped", reason))

    def check_blocked() -> None:
        for a in planned:
            if (not scope or a.key in scope) and a.key in blocked:
                raise _Stop(f"resolve key {a.key} first ({blocked[a.key]}); run `tree-resolve --key K`, or "
                            "`tree-rollback --key K` if it carries an id")

    try:
        check_blocked()
        _reconcile(services, ledger, execute, out, held, blocked)
        check_blocked()
        for a in planned:
            if scope and a.key not in scope:
                continue
            if a.key in executed:
                skip(a, "already executed (ledger)")
                continue
            if a.key in held:
                skip(a, "interrupted earlier; reconciled this run")
                continue
            if writes >= max_actions:
                summary.truncated = True
                break
            try:
                ctx = _precheck(a, services, state, assumed)
            except _Skip as e:
                skip(a, str(e))
                continue
            except ReadFailed as e:
                raise _Stop(f"pre-check read failed: {type(e).__name__}") from e
            if not execute:
                writes += 1
                if a.kind == "add-couple":  # a dry run writes nothing, so assume its couples exist for later children
                    assumed.add(frozenset((a.params["a"], a.params["b"])))
                out.append(Outcome(a.key, a.kind, _who(a), "would-create", "dry run"))
                continue
            _write_one(a, _who(a), ctx, plan.plan_hash, services, ledger, out)
            executed.add(a.key)
            writes += 1
    except _Stop as e:
        summary.stopped, summary.stop_reason = True, str(e)
    except Exception as e:  # unexpected (e.g. expired token): stop, never crash past the ledger
        summary.stopped, summary.stop_reason = True, f"unexpected error: {_http_status(e)}"
    _warn(summary, ledger)
    if out and out[-1].status in ("failed", "failed-readback", "partial", "ambiguous"):
        summary.stopped = True
        summary.stop_reason = summary.stop_reason or out[-1].reason
    return summary


# ------------------------------------------------------------------------ rollback


def _rolled_ids(recs: list[dict], env: str) -> set[tuple[str, str, str]]:
    out = set()
    for r in recs:
        if r.get("type") == "rollback" and r.get("status") == "rolled-back" and r.get("env") == env:
            for field_, v in (r.get("created") or {}).items():
                out.add((r.get("key"), field_, v))
    return out


def _tree_rollback_targets(ledger: Ledger, env: str, keys: Sequence[str]) -> list[dict]:
    """(key, kind, params, created) records, newest first, not yet rolled back. Bulk: created only;
    named keys: anything carrying an id (partial, failed-readback, progress-only)."""
    recs = ledger.records()
    rolled = _rolled_ids(recs, env)
    wanted = set(keys)
    known = {r.get("key") for r in recs if r.get("env") == env and r.get("type") in ("result", "intent")}
    if wanted - known:
        raise GateError(f"key not in the tree ledger for {env}: {sorted(wanted - known)[0][:40]}")
    done_intents = {r.get("intent_id") for r in recs if r.get("type") == "result"}
    intents = {i.get("intent_id"): i for i in recs if i.get("type") == "intent"}
    out: list[dict] = []
    seen_progress: set[str] = set()
    for r in recs:
        if r.get("env") != env:
            continue
        progress_only = False
        if r.get("type") == "result" and isinstance(r.get("created"), dict) and r["created"]:
            if not wanted and r.get("status") != "created":
                continue
            created, params = r["created"], r.get("params") or {}
        elif r.get("type") == "progress" and r.get("intent_id") not in done_intents and wanted:
            if r.get("intent_id") in seen_progress:
                continue
            seen_progress.add(r.get("intent_id"))
            created = _progress_for(recs, r.get("intent_id"))
            params = (intents.get(r.get("intent_id")) or {}).get("params") or {}
            progress_only = True
        else:
            continue
        if wanted and r.get("key") not in wanted:
            continue
        kind = r.get("kind") or (intents.get(r.get("intent_id")) or {}).get("kind")
        pending = {k: v for k, v in created.items() if (r.get("key"), k, v) not in rolled}
        if pending:
            out.append({"key": r.get("key"), "kind": kind, "params": params, "created": pending,
                        "intent_id": r.get("intent_id") if progress_only else None})
    out.reverse()  # newest first: relationships go before the person they hang on
    return out


def _owned_pairs(recs: list[dict], env: str) -> set[frozenset[str]]:
    """Person pairs joined by a ledger-created relationship that is not rolled back."""
    rolled = _rolled_ids(recs, env)
    pairs = set()
    for r in recs:
        if r.get("type") != "result" or r.get("env") != env:
            continue
        created, p = r.get("created") or {}, r.get("params") or {}
        if "couple_id" in created and (r["key"], "couple_id", created["couple_id"]) not in rolled:
            pairs.add(frozenset((p.get("a"), p.get("b"))))
        if "child_parents_id" in created and (r["key"], "child_parents_id", created["child_parents_id"]) not in rolled:
            for par in p.get("parents") or []:
                pairs.add(frozenset((p.get("child"), par)))
    return pairs


def _person_rollback_refusal(r, oid: str, p: dict) -> str | None:
    """Why this created person must NOT be deleted by us (None when it holds only our own work)."""
    if r.get_source_ids(oid) or r.get_memory_ids(oid):
        return "person has sources or memories added by someone; manual on the website"
    summary = r.get_change_summary(oid)
    me = r.current_user_ids()  # compared internally, never printed
    if not summary or any(w not in me for w, _ in summary):
        return "person's change history has other contributors or unknown ones; manual on the website"
    if any("merge" in text for _, text in summary):
        return "person's change history shows a merge; manual on the website"
    return None


def _couple_rollback_refusal(r, oid: str, pair: tuple, ignore_children: set) -> str | None:
    """Why this couple must NOT be deleted by us (None when nothing of anyone else's depends on it)."""
    a_, b_ = pair
    for c in [rid for rel, rid in _rels(r, a_, strict=False) if rel == "child"]:
        if c in ignore_children:
            continue
        if ("parent", b_) in _rels(r, c, strict=False):
            return "a child-and-parents relationship names both people as parents; manual on the website"
    extras = r.get_couple_extras(oid)
    if extras["facts"] or extras["sources"] or extras["notes"]:
        return "the couple has facts, sources or notes; manual on the website"
    me = r.current_user_ids()  # compared internally, never printed
    changes = extras["changes"]
    if not changes or any(w not in me for w, _ in changes):
        return "the couple's change history has other contributors or unknown ones; manual on the website"
    if any("merge" in text for _, text in changes):
        return "the couple's change history shows a merge; manual on the website"
    return None


def _tree_blocked(ledger: Ledger, env: str) -> dict[str, str]:
    """Blocked keys. Unlike Ledger.blocked_keys, ONE rolled-back line never lifts a key that still carries
    a recorded id that has not itself been rolled back."""
    blocked = dict(ledger.blocked_keys(env))
    recs = ledger.records()
    rolled = _rolled_ids(recs, env)
    ok_intents = {r.get("intent_id") for r in recs if r.get("type") == "result" and r.get("status") == "created"}
    has_result = {r.get("intent_id") for r in recs if r.get("type") == "result"}  # others: reconcile's job
    for r in recs:
        key = r.get("key")
        if r.get("env") != env or not isinstance(key, str):
            continue
        carries = (r.get("type") == "result" and r.get("status") != "created") or \
            (r.get("type") == "progress" and r.get("intent_id") in has_result
                                              and r.get("intent_id") not in ok_intents)
        if carries and any((key, f, v) not in rolled for f, v in (r.get("created") or {}).items()):
            blocked.setdefault(key, "rollback-incomplete")
    return blocked


def rollback_tree(ledger: Ledger, services: TreeServices, keys: Sequence[str] = (), execute: bool = False,
                  *, only: Sequence[str] = (), allow_production: bool = False) -> RollbackSummary:
    """Delete only ledger-created ids, then read back that they are gone."""
    scope = check_tree_run_gates(services.env, allow_production, only, 1)
    if execute and not (ledger.path.parent / "execute.lock").is_file():
        raise GateError("execute requires the run lock to be held")
    env = services.env
    targets = [t for t in _tree_rollback_targets(ledger, env, keys) if not scope or t["key"] in scope]
    n_ids = sum(len(t["created"]) for t in targets)
    if env == "production" and n_ids > PRODUCTION_HARD_CAP:
        raise GateError(f"refusing to roll back more than {PRODUCTION_HARD_CAP} objects in production")
    out: list[Outcome] = []
    summary = RollbackSummary(execute, out)
    recs = ledger.records()
    r, w = services.reader, services.writer
    if execute:
        _require_guarded_writer(services)
    pending_children = {t["params"].get("child") for t in targets if t["kind"] == "add-child-parents"}

    for t in targets:
        key, kind, p = t["key"], t["kind"], t["params"]
        if execute and t.get("intent_id"):
            # a crash left progress ids with no result: give the intent one so reconcile cannot re-block it
            _result(ledger, intent_id=t["intent_id"], key=key, kind=kind or "", env=env, status="partial",
                    reason="interrupted after the write returned an id (recorded at rollback)",
                    created=t["created"], params=p, reconciled=True)
        for field_ in sorted(t["created"], key=lambda f: ROLLBACK_ORDER.index(f) if f in ROLLBACK_ORDER else 99):
            oid = t["created"][field_]
            who = _who(TreeAction(kind or "", key, p, "planned", ""))

            def done(status: str, reason: str) -> None:
                out.append(Outcome(key, kind or "", who, status, reason))
                if execute:
                    ledger._append({"type": "rollback", "ts": utc_now(), "key": key, "env": env,
                                    "status": "rolled-back" if status == "rolled-back" else "rollback-failed",
                                    "reason": reason, "created": {field_: oid}})

            if not valid_object_id(oid) or field_ not in ALLOWED_FIELDS.get(kind or "", ()):
                out.append(Outcome(key, kind or "", who, "cannot-rollback", "invalid id or kind in ledger"))
                continue
            try:
                if field_ == "couple_id":
                    pair = (p.get("a"), p.get("b"))
                    if not all(_is_id(x) for x in pair):
                        out.append(Outcome(key, kind, who, "cannot-rollback", "invalid person ids in ledger"))
                        continue
                    a_, b_ = pair
                    if r.get_couple(oid) != frozenset(pair):
                        done("rollback-failed", "recorded id does not name this couple (or is gone); "
                                                "verify by id manually")
                        continue
                    refusal = _couple_rollback_refusal(r, oid, pair, set() if execute else pending_children)
                    if refusal:
                        done("rollback-failed", refusal)
                        continue
                    if not execute:
                        out.append(Outcome(key, kind, who, "would-rollback", "dry run"))
                        continue
                    _delete(w.delete_couple, oid, done)
                    if r.get_couple(oid) is not None:  # absence by id first
                        done("rollback-failed", "absence read-back mismatch: couple still present")
                        raise _Stop("read-back mismatch")
                    gone = _try(lambda: ("spouse", b_) not in _rels(r, a_) and ("spouse", a_) not in _rels(r, b_))
                    if gone is False:
                        done("rollback-failed", "absence read-back mismatch: spouses still listed")
                        raise _Stop("read-back mismatch")
                    if gone is None:
                        done("rolled-back", "deleted (absent by id; relatives read-back unavailable)")
                        continue
                elif field_ == "child_parents_id":
                    child, parents = p.get("child"), p.get("parents") or []
                    if not (_is_id(child) and parents and all(_is_id(x) for x in parents)):
                        out.append(Outcome(key, kind, who, "cannot-rollback", "invalid person ids in ledger"))
                        continue
                    if r.get_child_and_parents(oid) != (child, frozenset(parents)):
                        done("rollback-failed", "recorded id does not name this child and these parents "
                                                "(or is gone); verify by id manually")
                        continue
                    if not execute:
                        out.append(Outcome(key, kind, who, "would-rollback", "dry run"))
                        continue
                    _delete(w.delete_child_and_parents, oid, done)
                    if r.get_child_and_parents(oid) is not None:  # absence by id first
                        done("rollback-failed", "absence read-back mismatch: relationship still present")
                        raise _Stop("read-back mismatch")
                    gone = _try(lambda: not any(rel == "parent" and rid in parents for rel, rid in _rels(r, child)))
                    if gone is False:
                        done("rollback-failed", "absence read-back mismatch: parents still listed")
                        raise _Stop("read-back mismatch")
                    if gone is None:
                        done("rolled-back", "deleted (absent by id; relatives read-back unavailable)")
                        continue
                else:
                    fs = r.get_fs_person(oid)
                    if fs.status != "ok" or fs.id != oid:
                        done("rollback-failed", f"person not readable ({fs.status}); verify by id manually")
                        continue
                    if _norm(fs.name) != _norm(p.get("name")):
                        done("rollback-failed", "person's name changed since we created it; manual on the website")
                        continue
                    owned = _owned_pairs(recs, env)
                    rels = _rels(r, oid, strict=False)
                    foreign = [rid for _, rid in rels if frozenset((oid, rid)) not in owned]
                    if foreign:
                        done("rollback-failed", "person has a relationship not created by this ledger; "
                                                "refusing to delete (manual on the website)")
                        continue
                    if rels and execute:
                        done("rollback-failed", "person still has our relationships; roll those back first")
                        continue
                    refusal = _person_rollback_refusal(r, oid, p)
                    if refusal:
                        done("rollback-failed", refusal)
                        continue
                    if not execute:
                        out.append(Outcome(key, kind, who, "would-rollback", "dry run"))
                        continue
                    _delete(w.delete_person, oid, done)
                    after = r.get_fs_person(oid)
                    if after.status not in ("not-found", "deleted"):
                        done("rollback-failed", f"absence read-back mismatch (status {after.status})")
                        raise _Stop("read-back mismatch")
                done("rolled-back", "deleted")
            except _Stop as e:
                summary.stopped, summary.stop_reason = True, str(e)
                break
            except Exception as e:  # incl. ReadFailed / expired token; a delete may have happened
                summary.stopped, summary.stop_reason = True, f"unexpected error: {_http_status(e)}"
                if execute:
                    done("rollback-failed", f"unexpected error: {_http_status(e)}; verify by id manually")
                break
        if summary.stopped:
            break
    _warn(summary, ledger)
    return summary


def _try(fn):
    """True/False from fn, or None when it could not be read (a secondary check only)."""
    try:
        return fn()
    except Exception:
        return None


def _delete(fn, oid: str, done) -> None:
    try:
        fn(oid, reason=ROLLBACK_REASON)
    except Exception as e:
        done("rollback-failed", f"delete error: {_http_status(e)}")
        raise _Stop("delete failed") from e


# ------------------------------------------------------------------------ resolve


def resolve_tree(ledger: Ledger, services: TreeServices, keys: Sequence[str], execute: bool = False,
                 *, only: Sequence[str] = (), allow_production: bool = False,
                 confirm_absent: Sequence[str] = ()) -> RunSummary:
    """Operator path out of a blocked key that carries no id.

    Couple / child-parents: the exact relationship now exists -> recorded ``created (reconciled)`` (no id,
    so not rollbackable by id); absent, intent at least 15 minutes old -> ``resolved``.
    Create-person: NEVER lifted by a search (index lag, ambiguous timeouts): only a key passed in
    ``confirm_absent`` (you checked FamilySearch by hand) is resolved; otherwise the raw search hits are shown.
    """
    scope = check_tree_run_gates(services.env, allow_production, only, 1)
    if execute and not (ledger.path.parent / "execute.lock").is_file():
        raise GateError("execute requires the run lock to be held")
    keys = list(dict.fromkeys(keys))
    if not keys:
        raise GateError("tree-resolve needs at least one --key")
    confirm = set(confirm_absent)
    if confirm - set(keys):
        raise GateError("--confirm-absent keys must also be given as --key")
    env = services.env
    if env == "production" and len(keys) > PRODUCTION_HARD_CAP:
        raise GateError(f"refusing to resolve more than {PRODUCTION_HARD_CAP} keys in production")
    blocked = _tree_blocked(ledger, env)
    for k in keys:
        if k not in blocked:
            raise GateError(f"key is not blocked in {env}: {k[:40]}")
    for k in confirm:
        if not k.startswith("create-person:"):
            raise GateError("--confirm-absent applies to create-person keys only")
    recs = ledger.records()
    rolled = _rolled_ids(recs, env)
    out: list[Outcome] = []
    summary = RunSummary(execute, out)
    reader = services.reader
    for k in keys:
        intents = [r for r in recs if r.get("type") == "intent" and r.get("key") == k
                   and Ledger._env_ok(r, env) and r.get("kind") in TREE_KINDS]
        if not intents:
            out.append(Outcome(k, "", "", "still-blocked", "no intent recorded; check FamilySearch by hand"))
            continue
        rec = intents[-1]
        kind, p, iid = rec["kind"], rec.get("params") or {}, rec.get("intent_id")
        who = _who(TreeAction(kind, k, p, "planned", ""))
        if scope and k not in scope:
            out.append(Outcome(k, kind, who, "skipped", "outside --only"))
            continue
        if not isinstance(iid, str):
            out.append(Outcome(k, kind, who, "still-blocked", "intent has no intent_id; check by hand"))
            continue
        has_id = any((k, f_, v) not in rolled
                     for r in recs if r.get("key") == k and r.get("type") in ("result", "progress")
                     and Ledger._env_ok(r, env)
                     for f_, v in (r.get("created") or {}).items())
        if has_id:
            out.append(Outcome(k, kind, who, "still-blocked", "carries an object id; use tree-rollback --key"))
            continue
        common = dict(intent_id=iid, key=k, kind=kind, env=env, params=p)
        if kind == "create-person":
            if k in confirm:
                try:
                    shown = _hit_text(_search_hits(reader, p)) or "none"
                except Exception as e:
                    shown = f"search unavailable ({type(e).__name__})"
                if execute:
                    ledger.resolved(key=k, env=env, reason="operator confirmed the person is not on FamilySearch")
                out.append(Outcome(k, kind, who, "resolved" if execute else "would-resolve",
                                   f"block lifted on your confirmation that the person is absent; "
                                   f"current search hits (deceased ids only): {shown}"))
                continue
            try:
                hits = _search_hits(reader, p)
                detail = f"raw search hits: {_hit_text(hits) or 'none'}"
            except Exception as e:
                detail = f"search unavailable ({type(e).__name__})"
            out.append(Outcome(k, kind, who, "still-blocked",
                               f"check FamilySearch by hand, then pass --confirm-absent {k}; {detail}"))
            continue
        try:
            if kind == "add-couple":
                present = ("spouse", p.get("b")) in _rels(reader, p["a"])
            else:
                present = {rid for rel, rid in _rels(reader, p["child"]) if rel == "parent"} == set(p["parents"])
        except Exception as e:
            summary.stopped, summary.stop_reason = True, f"read failed: {_http_status(e)}"
            break
        if present:  # exists, but nothing proves it is ours (no id in a progress line): never record it as ours
            out.append(Outcome(k, kind, who, "still-blocked",
                               "the relationship exists on FamilySearch but is not recorded as ours (no id); "
                               "check it by hand, or on the website"))
        elif intent_age_seconds(rec) >= MIN_SETTLE_SECONDS:
            if execute:
                ledger.resolved(key=k, env=env, reason="absent on FamilySearch, intent settled")
            out.append(Outcome(k, kind, who, "resolved" if execute else "would-resolve",
                               "absent on FamilySearch; block lifted"))
        else:
            out.append(Outcome(k, kind, who, "still-blocked",
                               "check FamilySearch by hand (intent too recent)"))
    _warn(summary, ledger)
    return summary


# ---------------------------------------------------------------- real services


class GuardedTreeWriter:
    """TreeWriter behind the generated-API registry lock: every call runs ``check_call`` for its
    operation first (verified-on-beta ops need ``allow_production`` against production; deletes need
    ``confirm_destructive``, which this wrapper supplies only for its own delete methods)."""

    OPS = {"create_couple": "createRelationship", "create_parent_child": "createRelationship",
           "create_person": "createPerson", "delete_couple": "deleteCoupleRelationship",
           "delete_child_and_parents": "deleteChildAndParentsRelationship", "delete_person": "deletePerson"}

    def __init__(self, inner, environment, allow_production: bool, registry: dict | None = None):
        from familysearch.api import OPERATIONS
        from familysearch.api.registry import load_registry
        self._inner = inner
        self._env = environment
        self._allow = allow_production is True
        self._registry = registry if registry is not None else load_registry()
        self._ops = OPERATIONS

    def _guard(self, name: str) -> None:
        from familysearch.api.registry import check_call
        check_call(self._ops[self.OPS[name]], self._env, self._registry, allow_production=self._allow,
                   confirm_destructive=name.startswith("delete_"))

    def create_couple(self, *a, **k):
        self._guard("create_couple")
        return self._inner.create_couple(*a, **k)

    def create_parent_child(self, *a, **k):
        self._guard("create_parent_child")
        return self._inner.create_parent_child(*a, **k)

    def create_person(self, *a, **k):
        self._guard("create_person")
        return self._inner.create_person(*a, **k)

    def delete_couple(self, *a, **k):
        self._guard("delete_couple")
        return self._inner.delete_couple(*a, **k)

    def delete_child_and_parents(self, *a, **k):
        self._guard("delete_child_and_parents")
        return self._inner.delete_child_and_parents(*a, **k)

    def delete_person(self, *a, **k):
        self._guard("delete_person")
        return self._inner.delete_person(*a, **k)


def build_tree_services(archive: Path, env: str, allow_production: bool) -> TreeServices:
    """Real services from Brave's session. Refuses unless the session belongs to ``env``."""
    from familysearch.auth import Environment, extract_brave_token
    from familysearch.client import FSClient
    from familysearch.discovery import Discovery
    from familysearch.tree import TreeWriter

    from .prod_reader import ReadOnlyFS

    environment = {"beta": Environment.BETA, "production": Environment.PRODUCTION}.get(env)
    if environment is None:
        raise GateError(f"unknown environment {env!r}")
    if env == "production" and allow_production is not True:
        raise GateError("production runs require --allow-production")
    token = extract_brave_token()
    reader = ReadOnlyFS.for_environment(environment, token_fn=lambda: token)  # session must match env
    client = FSClient(token, environment)  # separate client: the reader's is locked read-only
    writer = GuardedTreeWriter(TreeWriter(client, Discovery(client)), environment, allow_production)
    return TreeServices(reader=reader, writer=writer, archive=Path(archive), env=env)
