"""Import executor: the first code in the import engine that WRITES to FamilySearch.

Fail closed everywhere. Every gate here runs before any network I/O and before any
ledger ``intent`` line. This module never reads or writes the Brave session itself;
the CLI hands it already-built services.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from .planner import Action, PersonPlan, action_key, compute_plan_hash
from .planreport import load_approvals
from .record import is_inside, repo_root

REASON = "Added from family research archive"
PRODUCTION_HARD_CAP = 25
DEFAULT_MAX_ACTIONS = 10
ENVS = ("beta", "production")

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{3,4}$")
_ACTION_FIELDS = tuple(f.name for f in fields(Action))
_KINDS = ("upload-document", "add-source", "skip")
_STATES = ("planned", "likely-covered", "already-present", "skipped")


class GateError(Exception):
    """A safety gate refused the run. Nothing was written."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _forbid_repo(path: Path, what: str) -> None:
    if is_inside(path, repo_root()):
        raise GateError(f"refusing to use {what} inside the repo: {path}")


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class PlanFile:
    plan_hash: str
    actions: tuple[Action, ...]  # every action in the file, any state
    path: Path

    def planned(self) -> tuple[Action, ...]:
        return tuple(a for a in self.actions if a.state == "planned")


def _opt_str(v) -> bool:
    return v is None or isinstance(v, str)


def _safe_part(s: str) -> bool:
    return bool(s) and s not in (".", "..") and "/" not in s and "\\" not in s and "\0" not in s


def _parse_action(raw) -> Action:
    if not isinstance(raw, dict) or set(raw) != set(_ACTION_FIELDS):
        raise GateError("Plan.json has an action with unexpected fields")
    for name in ("key", "kind", "person_line", "person_folder", "fs_id", "state", "reason"):
        if not isinstance(raw[name], str):
            raise GateError(f"Plan.json action field {name} must be a string")
    for name in ("file", "title", "url", "sha256"):
        if not _opt_str(raw[name]):
            raise GateError(f"Plan.json action field {name} must be a string or null")
    if raw["kind"] not in _KINDS or raw["state"] not in _STATES:
        raise GateError("Plan.json action has an unknown kind or state")
    return Action(**raw)


def _validate_runnable(a: Action) -> None:
    """Structural checks for an action that could be executed. Re-derives the key."""
    if a.kind not in ("upload-document", "add-source"):
        raise GateError(f"planned action {a.key[:8]} has non-runnable kind {a.kind!r}")
    if not _ID_RE.match(a.fs_id or ""):
        raise GateError(f"planned action {a.key[:8]} has a malformed FamilySearch id")
    if not (_safe_part(a.person_line) and _safe_part(a.person_folder)):
        raise GateError(f"planned action {a.key[:8]} has an unsafe person path")
    if not a.title or not a.title.strip():
        raise GateError(f"planned action {a.key[:8]} has no title")
    if a.kind == "upload-document":
        if not a.file or a.file.startswith("/") or "\\" in a.file or "\0" in a.file \
                or any(p in ("", ".", "..") for p in a.file.split("/")):
            raise GateError(f"planned action {a.key[:8]} has an unsafe file path")
        if not a.sha256 or not _HASH_RE.match(a.sha256):
            raise GateError(f"planned action {a.key[:8]} has no valid sha256")
        expected = action_key(a.fs_id, "upload-document", a.sha256)
    else:
        if not a.url or not a.url.startswith(("https://", "http://")):
            raise GateError(f"planned action {a.key[:8]} has no valid source url")
        expected = action_key(a.fs_id, "add-source", a.url)
    if a.key != expected:
        raise GateError(f"planned action {a.key[:8]} key does not match its content")


def verify_plan_for_execution(archive: Path, plan_path: Path, confirm_hash: str) -> PlanFile:
    """Check every plan-level gate. Raises GateError; does no network I/O and writes nothing."""
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
            or not isinstance(data.get("people"), list):
        raise GateError("Plan.json is malformed")
    stored = data["plan_hash"]

    people: list[PersonPlan] = []
    for p in data["people"]:
        if not isinstance(p, dict) or not isinstance(p.get("actions"), list):
            raise GateError("Plan.json is malformed (person)")
        try:
            acts = tuple(_parse_action(a) for a in p["actions"])
            people.append(PersonPlan(p["line"], p["folder"], p.get("fs_id"), p["status"], acts,
                                     p.get("note", "")))
        except KeyError as e:
            raise GateError("Plan.json is malformed (person fields)") from e
    recomputed = compute_plan_hash(people)

    approvals_path = import_dir / "approvals.json"
    if not approvals_path.is_file():
        raise GateError("approvals.json not found; the plan has not been approved")
    try:
        load_approvals(approvals_path)  # validates shape
        approved = json.loads(approvals_path.read_text(encoding="utf-8")).get("approved_plan_hash")
    except (OSError, ValueError) as e:
        raise GateError(f"approvals.json is unreadable or malformed: {type(e).__name__}") from e
    if not isinstance(approved, str) or not _HASH_RE.match(approved):
        raise GateError("approvals.json has no valid approved_plan_hash")

    for label, value in (("stored plan_hash", stored), ("recomputed hash", recomputed),
                         ("approved_plan_hash", approved)):
        if not hmac.compare_digest(value.encode(), confirm_hash.encode()):
            raise GateError(f"--confirm-hash does not match the {label}")

    actions = tuple(a for p in people for a in p.actions)
    for a in actions:
        if a.state == "planned":
            _validate_runnable(a)
    return PlanFile(stored, actions, plan_path)


# ---------------------------------------------------------------------- run gates


def parse_only(only: Sequence[str]) -> frozenset[str]:
    out = set()
    for o in only:
        if not isinstance(o, str) or o.count("/") != 1:
            raise GateError(f"--only must be LINE/FOLDER, got {o!r}")
        line, folder = o.split("/")
        if not (_safe_part(line) and _safe_part(folder)):
            raise GateError(f"--only must be LINE/FOLDER, got {o!r}")
        out.add(o)
    return frozenset(out)


def check_run_gates(env: str, allow_production: bool, only: Sequence[str], max_actions: int) -> None:
    if env not in ENVS:
        raise GateError(f"--env must be beta or production, got {env!r}")
    if isinstance(max_actions, bool) or not isinstance(max_actions, int) or max_actions < 1:
        raise GateError("--max-actions must be a positive integer")
    scope = parse_only(only)
    if env == "production":
        if allow_production is not True:
            raise GateError("production runs require --allow-production")
        if not scope:
            raise GateError("production runs require --only LINE/FOLDER (no all-people run)")
        if max_actions > PRODUCTION_HARD_CAP:
            raise GateError(f"--max-actions may not exceed {PRODUCTION_HARD_CAP} in production")


# ------------------------------------------------------------------------- ledger


class Ledger:
    """Append-only JSONL: ``intent`` before each write, ``result`` after. Never inside the repo."""

    def __init__(self, path: Path):
        self.path = Path(path)
        _forbid_repo(self.path, "ledger")
        self.corrupt_lines = 0

    def _append(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        if self.path.exists() and self.path.stat().st_size:
            with open(self.path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":  # torn last line from a crash: start a fresh one
                    prefix = "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(prefix + line)
            f.flush()
            os.fsync(f.fileno())

    def records(self) -> list[dict]:
        self.corrupt_lines = 0
        if not self.path.exists():
            return []
        out = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                self.corrupt_lines += 1  # a torn line is an unfinished write; never a "created"
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def executed_keys(self) -> frozenset[str]:
        return frozenset(r["key"] for r in self.records()
                         if r.get("type") == "result" and r.get("status") == "created"
                         and isinstance(r.get("key"), str))

    def intent(self, *, action: Action, env: str, plan_hash: str) -> str:
        """Append an intent line (flushed to disk) BEFORE the write; returns its intent id."""
        intent_id = uuid.uuid4().hex
        self._append({
            "type": "intent", "intent_id": intent_id, "ts": utc_now(), "key": action.key,
            "kind": action.kind, "fs_id": action.fs_id, "env": env, "plan_hash": plan_hash,
            "file": action.file, "title": action.title, "url": action.url, "sha256": action.sha256,
        })
        return intent_id

    def result(self, *, intent_id: str, key: str, status: str, reason: str,
               kind: str | None = None, fs_id: str | None = None, env: str | None = None,
               title: str | None = None, url: str | None = None, memory_id: str | None = None,
               source_description_id: str | None = None, reconciled: bool = False) -> None:
        if status not in ("created", "skipped", "failed", "failed-readback"):
            raise ValueError(f"bad result status {status!r}")
        self._append({
            "type": "result", "intent_id": intent_id, "ts": utc_now(), "key": key,
            "status": status, "reason": reason, "kind": kind, "fs_id": fs_id, "env": env,
            "title": title, "url": url, "memory_id": memory_id,
            "source_description_id": source_description_id, "reconciled": reconciled,
        })

    def rollback_result(self, *, key: str, status: str, reason: str, env: str,
                        memory_id: str | None = None,
                        source_description_id: str | None = None) -> None:
        if status not in ("rolled-back", "rollback-failed"):
            raise ValueError(f"bad rollback status {status!r}")
        self._append({
            "type": "rollback", "ts": utc_now(), "key": key, "status": status, "reason": reason,
            "env": env, "memory_id": memory_id, "source_description_id": source_description_id,
        })

    def unresolved_intents(self) -> list[dict]:
        recs = self.records()
        done = {r.get("intent_id") for r in recs if r.get("type") == "result"}
        return [r for r in recs if r.get("type") == "intent" and r.get("intent_id") not in done]


# --------------------------------------------------------------------------- lock


class RunLock:
    """Exclusive ``<archive>/_import/execute.lock``. Refuses if present; ``break_lock`` clears it."""

    def __init__(self, archive: Path, *, break_lock: bool = False,
                 announce: Callable[[str], None] = print):
        self.path = Path(archive) / "_import" / "execute.lock"
        _forbid_repo(self.path, "lock file")
        self._break = break_lock
        self._announce = announce
        self._held = False

    def _read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return "(unreadable)"

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._break and self.path.exists():
            self._announce(f"breaking lock {self.path}: {self._read()}")
            try:
                self.path.unlink()
            except OSError as e:
                raise GateError(f"cannot remove lock: {type(e).__name__}") from e
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
        except FileExistsError:
            raise GateError(f"another run holds {self.path}: {self._read()}; "
                            "use --break-lock only if it is stale") from None
        except OSError as e:
            raise GateError(f"cannot create lock: {type(e).__name__}") from e
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "started": utc_now()}, f)
        self._held = True
        return self

    def __exit__(self, *exc) -> None:
        if self._held:
            self._held = False
            try:
                self.path.unlink()
            except OSError:
                pass
