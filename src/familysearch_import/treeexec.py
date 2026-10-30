"""Tree executor: writes approved couples, parent links and new deceased people to FamilySearch.

Mirrors execute.py's safety model (dry run by default, plan hash + approval, run lock, ledger with
intent/progress/result, fresh pre-checks, read-back with a negative control, rollback by recorded
ids only). Every gate runs before network I/O and before any ledger line.

Tree runs use their own ledger file (``tree-ledger.jsonl``) so upload reconciliation never sees tree
intents (and vice versa); the run lock ``execute.lock`` is shared, so the two never run together.

Known limitation: a two-parent child-and-parents relationship makes FamilySearch derive a couple link
between the parents. That derived couple is a side effect we did not create by id; rollback deletes the
child-and-parents relationship only and does not touch the derived couple.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path

from .execute import (
    DEFAULT_MAX_ACTIONS, ENVS, MIN_SETTLE_SECONDS, PRODUCTION_HARD_CAP, REASON, GateError, Ledger,
    Outcome, RunSummary, _forbid_repo, _http_status, _warn, check_run_gates, intent_age_seconds,
    utc_now, valid_object_id,
)
from .execute import RollbackSummary
from .prod_reader import ReadFailed
from .record import is_inside
from .scan import Person
from .treeplan import LIVING_CUTOFF, TreeAction, compute_tree_plan_hash, load_tree_approvals, _linked_ids

ROLLBACK_REASON = "Rolled back import from family research archive"
TREE_KINDS = ("add-couple", "add-child-parents", "create-person")
_STATES = ("planned", "skipped", "manual")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{3,4}$")
_ACTION_FIELDS = tuple(f.name for f in fields(TreeAction))
_RESULT_STATUSES = ("created", "skipped", "failed", "failed-readback", "partial", "ambiguous")
LEDGER_NAME = "tree-ledger.jsonl"
GX = "http://gedcomx.org/"
CREATED_FIELD = {"add-couple": "couple_id", "add-child-parents": "child_parents_id", "create-person": "person_id"}


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class TreePlanFile:
    plan_hash: str
    env: str
    actions: tuple[TreeAction, ...]
    path: Path
    verified: bool = False  # set only by verify_tree_plan_for_execution

    def planned(self) -> tuple[TreeAction, ...]:
        return tuple(a for a in self.actions if a.state == "planned")


def _norm(s: str | None) -> str:
    from .planner import _norm as n
    return n(s)


def _is_year(v) -> bool:
    return v is None or (isinstance(v, int) and not isinstance(v, bool) and 0 < v < 3000)


def _is_id(v) -> bool:
    return isinstance(v, str) and bool(_ID_RE.match(v))


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
        if a.key != f"spouse:{x}:{y}":
            raise GateError(f"planned action {tag} key does not match its content")
    elif a.kind == "add-child-parents":
        child, parents = p.get("child"), p.get("parents")
        if not _is_id(child) or not isinstance(parents, list) or not 1 <= len(parents) <= 2 \
                or not all(_is_id(x) for x in parents) or len(set(parents)) != len(parents) \
                or child in parents:
            raise GateError(f"planned action {tag} has malformed child/parent ids")
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
        if a.key != "create-person:" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]:
            raise GateError(f"planned action {tag} key does not match its content")
    else:
        raise GateError(f"planned action {tag} has non-runnable kind {a.kind!r}")


def _deceased_by_rule(birth, death) -> bool:
    if death is not None:
        return True
    return birth is not None and birth < LIVING_CUTOFF


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
        load_tree_approvals(approvals_path)  # validates shape
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
    """reader: get_fs_person(id), get_relatives(id), search_candidates(person, limit);
    writer: create_couple, create_parent_child, create_person, delete_couple,
    delete_child_and_parents, delete_person (see familysearch.tree.TreeWriter)."""
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
    for r in recs:
        if r.get("type") == "progress" and r.get("intent_id") == intent_id and isinstance(r.get("created"), dict):
            return r["created"]
    return {}


def _tree_intents(ledger: Ledger, env: str) -> list[dict]:
    return [r for r in ledger.unresolved_intents(env) if r.get("kind") in TREE_KINDS]


# ----------------------------------------------------------------- pre-checks


class _Stop(Exception):
    """Internal: stop the run (fail closed)."""


class _Skip(Exception):
    """Internal: a fresh pre-check refused this action; no write."""


def _linked_now(archive: Path) -> frozenset[str]:
    """All currently linked FamilySearch ids (links.json confirmed + approved_links). Fail closed."""
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
    return _linked_ids(links, approvals)


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


def _rels(reader, pid: str) -> list[tuple[str, str]]:
    return [(r.relationship, r.fs.id) for r in reader.get_relatives(pid)]


def _pick_control(reader, linked: frozenset[str], exclude: set[str]) -> str:
    """An unrelated, deceased, linked person for the negative control. Raises _Skip if none."""
    for cid in sorted(linked - exclude)[:8]:
        try:
            _check_person(reader, cid, "control person")
        except _Skip:
            continue
        if not any(rid in exclude for _, rid in _rels(reader, cid)):
            return cid
    raise _Skip("no unrelated linked person available for the negative control")


def _precheck(a: TreeAction, services: TreeServices, linked: frozenset[str]) -> dict:
    """Fresh reads right before the write. Returns context for the read-back; raises _Skip/_Stop."""
    r, p = services.reader, a.params
    if a.kind == "add-couple":
        x, y = p["a"], p["b"]
        for pid in (x, y):
            if pid not in linked:
                raise _Skip("an end is no longer a confirmed link in links.json")
        for pid in (x, y):
            _check_person(r, pid, f"person {pid}")
        if ("spouse", y) in _rels(r, x) or ("spouse", x) in _rels(r, y):
            raise _Skip("a couple relationship between them already exists on FamilySearch")
        return {"control": _pick_control(r, linked, {x, y})}
    if a.kind == "add-child-parents":
        child, parents = p["child"], list(p["parents"])
        for pid in [child, *parents]:
            if pid not in linked:
                raise _Skip("an id is no longer a confirmed link in links.json")
        for pid in [child, *parents]:
            _check_person(r, pid, f"person {pid}")
        if any(rel == "parent" for rel, _ in _rels(r, child)):
            raise _Skip("child already has a parent relationship on FamilySearch")
        return {"control": _pick_control(r, linked, {child, *parents})}
    # create-person
    if not _deceased_by_rule(p.get("birth"), p.get("death")):
        raise _Skip("person is not deceased by the archive rule")
    if p.get("gender") != "Unknown":
        raise _Skip("gender must be Unknown")
    person = Person(line="", folder="", path=Path("."), birth=p.get("birth"), death=p.get("death"),
                    display_name=p["name"], files=(), familysearch_ids=(), book_excluded=frozenset())
    found = r.search_candidates(person, 10)
    if found:
        raise _Skip("plausible match " + ", ".join(sorted({c.id for c in found})))
    return {}


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


def _readback(a: TreeAction, new_id: str, ctx: dict, services: TreeServices) -> str | None:
    """None when the new state is proven AND the negative control is untouched; else a reason."""
    r, p = services.reader, a.params
    if a.kind == "add-couple":
        x, y = p["a"], p["b"]
        if ("spouse", y) not in _rels(r, x) or ("spouse", x) not in _rels(r, y):
            return "read-back: the two people do not list each other as spouse"
        if any(rid in (x, y) for _, rid in _rels(r, ctx["control"])):
            return "negative control failed: an unrelated person now lists one of them"
        return None
    if a.kind == "add-child-parents":
        child, parents = p["child"], set(p["parents"])
        got = {rid for rel, rid in _rels(r, child) if rel == "parent"}
        if got != parents:
            return "read-back: child's parents are not exactly the expected parents"
        for par in parents:
            if ("child", child) not in _rels(r, par):
                return "read-back: a parent does not list the child"
        if any(rid == child or rid in parents for _, rid in _rels(r, ctx["control"])):
            return "negative control failed: an unrelated person now lists the child or a parent"
        return None
    fs = r.get_fs_person(new_id)
    if fs.status != "ok" or fs.id != new_id:
        return "read-back: the new person is not readable"
    if _norm(fs.name) != _norm(p["name"]):
        return "read-back: the new person's name does not match"
    if fs.living is True:
        return "read-back: the new person is marked living"
    if _rels(r, new_id):
        return "read-back: the new person already has relationships"
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
        except Exception as e:  # any write error: record and stop; never retry blindly
            fail("failed", f"write error: {_http_status(e)}")
        if not valid_object_id(new_id):
            fail("failed-readback", "write returned a missing or invalid id")
        created[CREATED_FIELD[a.kind]] = new_id
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
    linked = _linked_now(services.archive)

    out: list[Outcome] = []
    summary = RunSummary(execute, out)
    executed = set(ledger.executed_keys(services.env))
    blocked = ledger.blocked_keys(services.env)
    held: set[str] = set()
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
                ctx = _precheck(a, services, linked)
            except _Skip as e:
                skip(a, str(e))
                continue
            except ReadFailed as e:
                raise _Stop(f"pre-check read failed: {type(e).__name__}") from e
            if not execute:
                writes += 1
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
    if out and out[-1].status in ("failed", "failed-readback", "partial"):
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
    out: list[dict] = []
    for r in recs:
        if r.get("env") != env:
            continue
        if r.get("type") == "result" and isinstance(r.get("created"), dict) and r["created"]:
            if not wanted and r.get("status") != "created":
                continue
            created, params = r["created"], r.get("params") or {}
        elif r.get("type") == "progress" and r.get("intent_id") not in done_intents and wanted:
            created = r.get("created") or {}
            params = next((i.get("params") or {} for i in recs
                           if i.get("type") == "intent" and i.get("intent_id") == r.get("intent_id")), {})
        else:
            continue
        if wanted and r.get("key") not in wanted:
            continue
        kind = r.get("kind") or next((i.get("kind") for i in recs if i.get("intent_id") == r.get("intent_id")
                                      and i.get("type") == "intent"), None)
        pending = {k: v for k, v in created.items() if (r.get("key"), k, v) not in rolled}
        if pending:
            out.append({"key": r.get("key"), "kind": kind, "params": params, "created": pending})
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

    for t in targets:
        key, kind, p = t["key"], t["kind"], t["params"]
        for field_, oid in t["created"].items():
            who = _who(TreeAction(kind or "", key, p, "planned", ""))

            def done(status: str, reason: str) -> None:
                out.append(Outcome(key, kind or "", who, status, reason))
                if execute:
                    ledger._append({"type": "rollback", "ts": utc_now(), "key": key, "env": env,
                                    "status": "rolled-back" if status == "rolled-back" else "rollback-failed",
                                    "reason": reason, "created": {field_: oid}})

            if not valid_object_id(oid) or field_ not in CREATED_FIELD.values() \
                    or CREATED_FIELD.get(kind or "") != field_:
                out.append(Outcome(key, kind or "", who, "cannot-rollback", "invalid id or kind in ledger"))
                continue
            try:
                if field_ == "couple_id":
                    a_, b_ = p.get("a"), p.get("b")
                    if not (_is_id(a_) and _is_id(b_)):
                        out.append(Outcome(key, kind, who, "cannot-rollback", "invalid person ids in ledger"))
                        continue
                    if ("spouse", b_) not in _rels(r, a_):
                        done("rollback-failed", "couple not found on FamilySearch; verify by id manually")
                        continue
                    if not execute:
                        out.append(Outcome(key, kind, who, "would-rollback", "dry run"))
                        continue
                    _delete(w.delete_couple, oid, done)
                    if ("spouse", b_) in _rels(r, a_) or ("spouse", a_) in _rels(r, b_):
                        done("rollback-failed", "absence read-back mismatch: couple still listed")
                        raise _Stop("read-back mismatch")
                elif field_ == "child_parents_id":
                    child, parents = p.get("child"), p.get("parents") or []
                    if not (_is_id(child) and parents and all(_is_id(x) for x in parents)):
                        out.append(Outcome(key, kind, who, "cannot-rollback", "invalid person ids in ledger"))
                        continue
                    got = {rid for rel, rid in _rels(r, child) if rel == "parent"}
                    if got != set(parents):
                        done("rollback-failed", "child's parents differ from what we created; verify manually")
                        continue
                    if not execute:
                        out.append(Outcome(key, kind, who, "would-rollback", "dry run"))
                        continue
                    _delete(w.delete_child_and_parents, oid, done)
                    if any(rel == "parent" and rid in parents for rel, rid in _rels(r, child)):
                        done("rollback-failed", "absence read-back mismatch: parents still listed")
                        raise _Stop("read-back mismatch")
                else:
                    fs = r.get_fs_person(oid)
                    if fs.status != "ok" or fs.id != oid:
                        done("rollback-failed", f"person not readable ({fs.status}); verify by id manually")
                        continue
                    owned = _owned_pairs(recs, env)
                    rels = _rels(r, oid)
                    foreign = [rid for _, rid in rels if frozenset((oid, rid)) not in owned]
                    if foreign:
                        done("rollback-failed", "person has a relationship not created by this ledger; "
                                                "refusing to delete")
                        continue
                    if rels and execute:
                        done("rollback-failed", "person still has our relationships; roll those back first")
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


def _delete(fn, oid: str, done) -> None:
    try:
        fn(oid, reason=ROLLBACK_REASON)
    except Exception as e:
        done("rollback-failed", f"delete error: {_http_status(e)}")
        raise _Stop("delete failed") from e


# ------------------------------------------------------------------------ resolve


def resolve_tree(ledger: Ledger, services: TreeServices, keys: Sequence[str], execute: bool = False,
                 *, only: Sequence[str] = (), allow_production: bool = False) -> RunSummary:
    """Operator path out of a blocked key that carries no id.

    Couple / child-parents: the relationship now exists -> recorded ``created (reconciled)`` (no id, so
    not rollbackable by id); absent, intent at least 15 minutes old -> ``resolved``. Create-person: a
    search that still finds nothing and a settled intent -> ``resolved``; any match -> stays blocked.
    """
    scope = check_tree_run_gates(services.env, allow_production, only, 1)
    if execute and not (ledger.path.parent / "execute.lock").is_file():
        raise GateError("execute requires the run lock to be held")
    keys = list(dict.fromkeys(keys))
    if not keys:
        raise GateError("tree-resolve needs at least one --key")
    env = services.env
    if env == "production" and len(keys) > PRODUCTION_HARD_CAP:
        raise GateError(f"refusing to resolve more than {PRODUCTION_HARD_CAP} keys in production")
    blocked = ledger.blocked_keys(env)
    for k in keys:
        if k not in blocked:
            raise GateError(f"key is not blocked in {env}: {k[:40]}")
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
        has_id = _progress_for(recs, iid) or any(
            r.get("type") == "result" and r.get("key") == k and Ledger._env_ok(r, env)
            and any((k, f_, v) not in rolled for f_, v in (r.get("created") or {}).items()) for r in recs)
        if has_id:
            out.append(Outcome(k, kind, who, "still-blocked", "carries an object id; use tree-rollback --key"))
            continue
        common = dict(intent_id=iid, key=k, kind=kind, env=env, params=p)
        try:
            if kind == "add-couple":
                present = ("spouse", p.get("b")) in _rels(reader, p["a"])
            elif kind == "add-child-parents":
                present = any(rel == "parent" and rid in (p.get("parents") or [])
                              for rel, rid in _rels(reader, p["child"]))
            else:
                person = Person(line="", folder="", path=Path("."), birth=p.get("birth"), death=p.get("death"),
                                display_name=p["name"], files=(), familysearch_ids=(),
                                book_excluded=frozenset())
                present = bool(reader.search_candidates(person, 10))
        except Exception as e:
            summary.stopped, summary.stop_reason = True, f"read failed: {_http_status(e)}"
            break
        if present and kind != "create-person":
            if execute:
                _result(ledger, status="created", reason="created (reconciled)", reconciled=True, **common)
            out.append(Outcome(k, kind, who, "resolved-created" if execute else "would-resolve-created",
                               "found on FamilySearch; recorded as created (no id)"))
        elif not present and intent_age_seconds(rec) >= MIN_SETTLE_SECONDS:
            if execute:
                ledger.resolved(key=k, env=env, reason="absent on FamilySearch, intent settled")
            out.append(Outcome(k, kind, who, "resolved" if execute else "would-resolve",
                               "absent on FamilySearch; block lifted"))
        else:
            out.append(Outcome(k, kind, who, "still-blocked",
                               "check FamilySearch by hand (present, or intent too recent)"))
    _warn(summary, ledger)
    return summary


# ---------------------------------------------------------------- real services


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
    return TreeServices(reader=reader, writer=TreeWriter(client, Discovery(client)),
                        archive=Path(archive), env=env)
