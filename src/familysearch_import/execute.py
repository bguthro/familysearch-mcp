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
    verified: bool = False  # set only by verify_plan_for_execution

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
    return PlanFile(stored, actions, plan_path, verified=True)


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

    @staticmethod
    def _env_ok(rec: dict, env: str | None) -> bool:
        # A line with no env is treated as matching every env (conservative).
        return env is None or rec.get("env") in (None, env)

    def executed_keys(self, env: str | None = None) -> frozenset[str]:
        return frozenset(r["key"] for r in self.records()
                         if r.get("type") == "result" and r.get("status") == "created"
                         and isinstance(r.get("key"), str) and self._env_ok(r, env))

    def intent(self, *, action: Action, env: str, plan_hash: str) -> str:
        """Append an intent line (flushed to disk) BEFORE the write; returns its intent id."""
        intent_id = uuid.uuid4().hex
        self._append({
            "type": "intent", "intent_id": intent_id, "ts": utc_now(), "key": action.key,
            "kind": action.kind, "fs_id": action.fs_id, "env": env, "plan_hash": plan_hash,
            "person": f"{action.person_line}/{action.person_folder}",
            "file": action.file, "title": action.title, "url": action.url, "sha256": action.sha256,
        })
        return intent_id

    def result(self, *, intent_id: str, key: str, status: str, reason: str,
               kind: str | None = None, fs_id: str | None = None, env: str | None = None,
               title: str | None = None, url: str | None = None, person: str | None = None,
               memory_id: str | None = None,
               source_description_id: str | None = None, reconciled: bool = False) -> None:
        if status not in ("created", "skipped", "failed", "failed-readback"):
            raise ValueError(f"bad result status {status!r}")
        self._append({
            "type": "result", "intent_id": intent_id, "ts": utc_now(), "key": key,
            "status": status, "reason": reason, "kind": kind, "fs_id": fs_id, "env": env,
            "title": title, "url": url, "person": person, "memory_id": memory_id,
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

    def unresolved_intents(self, env: str | None = None) -> list[dict]:
        recs = self.records()
        done = {r.get("intent_id") for r in recs if r.get("type") == "result"}
        return [r for r in recs if r.get("type") == "intent" and r.get("intent_id") not in done
                and self._env_ok(r, env)]


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


# ------------------------------------------------------------------------- engine

CONTENT_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png"}


@dataclass
class ExecServices:
    """Everything the engine may touch. ``reader`` is read-only; writes only via memories/sources.

    reader:   get_fs_person(id), get_memory_titles(id), get_source_refs(id)
    memories: upload_to_person(person_id, data, *, filename, content_type, title, reason) -> id;
              delete(memory_id, *, reason)
    sources:  create_description(about_url, title, citation, *, reason) -> id;
              attach_to_person(person_id, source_description_id, *, reason);
              delete(source_description_id, *, reason)
    """
    reader: object
    memories: object
    sources: object
    archive: Path
    env: str


@dataclass
class Outcome:
    key: str
    kind: str
    person: str  # "<line>/<folder>"
    status: str  # created|would-create|skipped|failed|failed-readback|reconciled-created|reconciled-absent
    reason: str


@dataclass
class RunSummary:
    execute: bool
    outcomes: list[Outcome]
    stopped: bool = False
    stop_reason: str = ""
    truncated: bool = False  # --max-actions reached; not a failure

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


class _Stop(Exception):
    """Internal: stop the run (fail closed)."""


def _norm(s: str | None) -> str:
    from .planner import _norm as n
    return n(s)


def _http_status(e: Exception) -> str:
    st = getattr(e, "status", None)
    return f"{type(e).__name__}" + (f" HTTP {st}" if isinstance(st, int) else "")


class _Archive:
    def __init__(self, root: Path):
        self.root = root
        self._lines: dict[str, list] = {}

    def person(self, line: str, folder: str):
        from .scan import nfc, scan_archive
        if line not in self._lines:
            self._lines[line] = scan_archive(self.root, [line])
        for p in self._lines[line]:
            if p.line == nfc(line) and p.folder == nfc(folder):
                return p
        return None


def _find_file(person, rel: str):
    from .planner import _rel
    from .scan import nfc
    for f in person.files:
        if _rel(person, f) == nfc(rel):
            return f
    return None


def _present_in(reader, a: Action) -> tuple[bool, int]:
    """(already on FamilySearch, current count). Read failures propagate as ReadFailed."""
    if a.kind == "upload-document":
        titles = reader.get_memory_titles(a.fs_id)
        return _norm(a.title) in {_norm(t) for t in titles}, len(titles)
    refs = reader.get_source_refs(a.fs_id)
    hit = any((a.url and r.url == a.url) or (r.title and _norm(r.title) == _norm(a.title)) for r in refs)
    return hit, len(refs)


def _reconcile(plan_actions: dict[str, Action], services: ExecServices, ledger: Ledger,
               execute: bool, out: list[Outcome], executed: set[str]) -> None:
    """Resolve intents with no result: read FamilySearch; never re-upload blindly."""
    for rec in ledger.unresolved_intents(services.env):
        key, fs_id = rec.get("key"), rec.get("fs_id")
        probe = Action(key or "", rec.get("kind") or "", "", "", fs_id or "", rec.get("file"),
                       rec.get("title"), rec.get("url"), "planned", "", rec.get("sha256"))
        if not (isinstance(key, str) and _ID_RE.match(fs_id or "")) \
                or probe.kind not in ("upload-document", "add-source"):
            raise _Stop("an unresolved ledger intent is malformed; resolve it manually")
        present, _ = _present_in(services.reader, probe)  # ReadFailed -> stop in caller
        who = f"{probe.kind}"
        if present:
            if execute:
                ledger.result(intent_id=rec["intent_id"], key=key, status="created",
                              reason="created (reconciled)", kind=probe.kind, fs_id=fs_id, person=rec.get("person"),
                              env=services.env, title=probe.title, url=probe.url, reconciled=True)
            executed.add(key)
            out.append(Outcome(key, who, "", "reconciled-created",
                               "interrupted run: object found on FamilySearch"))
        else:
            if execute:
                ledger.result(intent_id=rec["intent_id"], key=key, status="skipped",
                              reason="interrupted before write; absent on FamilySearch",
                              kind=probe.kind, fs_id=fs_id, person=rec.get("person"), env=services.env,
                              title=probe.title, url=probe.url)
            out.append(Outcome(key, who, "", "reconciled-absent",
                               "interrupted run: object absent, eligible again"))


def run_plan(plan: PlanFile, services: ExecServices, ledger: Ledger, *, only: Sequence[str] = (),
             max_actions: int = DEFAULT_MAX_ACTIONS, execute: bool = False,
             allow_production: bool = False) -> RunSummary:
    # Gates first: nothing below runs (no reads, no ledger lines) if any refuses.
    check_run_gates(services.env, allow_production, only, max_actions)
    if not isinstance(plan, PlanFile) or not plan.verified:
        raise GateError("plan was not verified by verify_plan_for_execution")
    if execute and not (ledger.path.parent / "execute.lock").is_file():
        raise GateError("execute requires the run lock to be held")
    scope = parse_only(only)
    planned = plan.planned()
    for a in planned:
        _validate_runnable(a)

    out: list[Outcome] = []
    summary = RunSummary(execute, out)
    executed = set(ledger.executed_keys(services.env))
    seen_titles: set[tuple[str, str, str]] = set()
    archive = _Archive(Path(services.archive))
    writes = 0

    def skip(a: Action, reason: str) -> None:
        out.append(Outcome(a.key, a.kind, f"{a.person_line}/{a.person_folder}", "skipped", reason))

    try:
        from .prod_reader import ReadFailed
        try:
            _reconcile({a.key: a for a in planned}, services, ledger, execute, out, executed)
        except ReadFailed as e:
            raise _Stop(f"cannot reconcile interrupted run: {type(e).__name__}") from e

        for a in planned:
            who = f"{a.person_line}/{a.person_folder}"
            if scope and who not in scope:
                continue
            if a.key in executed:
                skip(a, "already executed (ledger or this run)")
                continue
            if writes >= max_actions:
                summary.truncated = True
                break
            person = archive.person(a.person_line, a.person_folder)
            if person is None:
                skip(a, "person folder not found in archive")
                continue
            from .record import record_state_and_status
            status, rec_state = record_state_and_status(person)
            if rec_state == "invalid":  # a record exists but cannot be trusted: fail closed
                skip(a, "ImportRecord.json is unreadable or invalid")
                continue
            if status != "deceased":
                skip(a, "archive status is no longer deceased")
                continue
            data = b""
            if a.kind == "upload-document":
                f = _find_file(person, a.file or "")
                if f is None:
                    skip(a, "file no longer in archive")
                    continue
                try:
                    data = f.read_bytes()
                except OSError:
                    skip(a, "file unreadable")
                    continue
                import hashlib
                if hashlib.sha256(data).hexdigest() != a.sha256:
                    skip(a, "file changed since planning")
                    continue
                ctype = CONTENT_TYPES.get(f.suffix.lower())
                if ctype is None:
                    skip(a, "unsupported file type")
                    continue
            # --- fresh FamilySearch reads
            try:
                fs = services.reader.get_fs_person(a.fs_id)
            except ReadFailed as e:
                raise _Stop(f"person read failed: {type(e).__name__}") from e
            if fs.status in ("redirected", "not-found", "deleted"):
                skip(a, f"person on FamilySearch is {fs.status}")
                continue
            if fs.status != "ok":
                raise _Stop(f"person read error for {who}")
            if getattr(fs, "id", None) != a.fs_id:
                skip(a, "person id no longer matches the plan")
                continue
            if fs.living is not False:
                skip(a, "person is living or living status unknown on FamilySearch")
                continue
            tkey = (a.fs_id, a.kind, _norm(a.title if a.kind == "upload-document" else (a.url or a.title)))
            try:
                present, before = _present_in(services.reader, a)
            except ReadFailed as e:
                raise _Stop(f"existing-object read failed: {type(e).__name__}") from e
            if present or tkey in seen_titles:
                skip(a, "already present on FamilySearch" if present else "duplicate within this run")
                continue
            if not execute:
                seen_titles.add(tkey)
                writes += 1
                out.append(Outcome(a.key, a.kind, who, "would-create", "dry run"))
                continue
            _write_one(a, who, data, ctype if a.kind == "upload-document" else "", before,
                       plan, services, ledger, out)
            executed.add(a.key)
            seen_titles.add(tkey)
            writes += 1
    except _Stop as e:
        summary.stopped, summary.stop_reason = True, str(e)
    except Exception as e:  # unexpected (e.g. expired token mid-run): stop, never crash past the ledger
        summary.stopped, summary.stop_reason = True, f"unexpected error: {_http_status(e)}"
    if out and out[-1].status in ("failed", "failed-readback"):
        summary.stopped = True
        summary.stop_reason = summary.stop_reason or out[-1].reason
    return summary


def _write_one(a: Action, who: str, data: bytes, ctype: str, before: int, plan: PlanFile,
               services: ExecServices, ledger: Ledger, out: list[Outcome]) -> None:
    """One write: intent, write, read-back, result. Any failure records it and stops the run."""
    from .prod_reader import ReadFailed
    intent_id = ledger.intent(action=a, env=services.env, plan_hash=plan.plan_hash)
    common = dict(intent_id=intent_id, key=a.key, kind=a.kind, fs_id=a.fs_id, env=services.env,
                  title=a.title, url=a.url, person=who)
    mem_id = sd_id = None

    def fail(status: str, reason: str) -> None:
        ledger.result(status=status, reason=reason, memory_id=mem_id,
                      source_description_id=sd_id, **common)
        out.append(Outcome(a.key, a.kind, who, status, reason))
        raise _Stop(reason)

    try:
        if a.kind == "upload-document":
            mem_id = services.memories.upload_to_person(
                a.fs_id, data, filename=Path(a.file or "").name, content_type=ctype,
                title=a.title, reason=REASON)
            if not isinstance(mem_id, str) or not mem_id:
                mem_id = None
                fail("failed-readback", "upload returned no memory id")
        else:
            sd_id = services.sources.create_description(a.url, a.title, a.title, reason=REASON)
            if not isinstance(sd_id, str) or not sd_id:
                sd_id = None
                fail("failed-readback", "create returned no source description id")
            services.sources.attach_to_person(a.fs_id, sd_id, reason=REASON)
    except _Stop:
        raise
    except Exception as e:  # any write error: record and stop; never retry blindly
        fail("failed", f"write error: {_http_status(e)}")

    try:
        present, after = _present_in(services.reader, a)
    except Exception as e:  # incl. expired token: the created id must still reach the ledger
        fail("failed-readback", f"read-back failed: {_http_status(e)}")
    if not present or after != before + 1:
        fail("failed-readback", f"read-back mismatch (before {before}, after {after}, present {present})")
    ledger.result(status="created", reason="created", memory_id=mem_id,
                  source_description_id=sd_id, **common)
    out.append(Outcome(a.key, a.kind, who, "created", "created"))


# ------------------------------------------------------------------------ rollback

ROLLBACK_REASON = "Rolled back import from family research archive"


@dataclass
class RollbackSummary:
    execute: bool
    outcomes: list[Outcome]
    stopped: bool = False
    stop_reason: str = ""


def _rollback_targets(ledger: Ledger, env: str, keys: Sequence[str]) -> list[dict]:
    """Ledger result lines this tool created, in this env, not yet rolled back."""
    recs = ledger.records()
    rolled = {(r.get("key"), r.get("memory_id"), r.get("source_description_id"))
              for r in recs if r.get("type") == "rollback" and r.get("status") == "rolled-back"}
    wanted = set(keys)
    known = {r.get("key") for r in recs if r.get("type") == "result"}
    missing = wanted - known
    if missing:
        raise GateError(f"key not in the ledger: {sorted(missing)[0][:12]}")
    out, seen = [], set()
    for r in recs:
        if r.get("type") != "result" or r.get("env") != env:
            continue
        status = r.get("status")
        # failed-readback objects exist but are unconfirmed: only on explicit request by key
        if not (status == "created" or (status == "failed-readback" and r.get("key") in wanted)):
            continue
        if wanted and r.get("key") not in wanted:
            continue
        ident = (r.get("key"), r.get("memory_id"), r.get("source_description_id"))
        if ident in rolled or ident in seen or not (ident[1] or ident[2]):
            continue
        if not (isinstance(r.get("fs_id"), str) and _ID_RE.match(r["fs_id"])):
            continue
        seen.add(ident)
        out.append(r)
    return out


def rollback(ledger: Ledger, services: ExecServices, keys: Sequence[str] = (), execute: bool = False,
             *, only: Sequence[str] = (), allow_production: bool = False) -> RollbackSummary:
    """Delete only objects whose ids this tool recorded as created, then read back absence."""
    check_run_gates(services.env, allow_production, only, 1)
    if execute and not (ledger.path.parent / "execute.lock").is_file():
        raise GateError("execute requires the run lock to be held")
    scope = parse_only(only)
    targets = [t for t in _rollback_targets(ledger, services.env, keys)
               if not scope or t.get("person") in scope]
    if services.env == "production" and len(targets) > PRODUCTION_HARD_CAP:
        raise GateError(f"refusing to roll back more than {PRODUCTION_HARD_CAP} objects in production")
    out: list[Outcome] = []
    summary = RollbackSummary(execute, out)
    from .prod_reader import ReadFailed
    for t in targets:
        is_mem = bool(t.get("memory_id"))
        oid = t["memory_id"] if is_mem else t["source_description_id"]
        a = Action(t["key"], "upload-document" if is_mem else "add-source", "", "", t["fs_id"],
                   None, t.get("title"), t.get("url"), "planned", "", None)
        who = t.get("person") or ""
        kind = "memory" if is_mem else "source"

        def done(status: str, reason: str) -> None:
            out.append(Outcome(t["key"], kind, who, status, reason))
            if execute:
                ledger.rollback_result(
                    key=t["key"], status="rolled-back" if status == "rolled-back" else "rollback-failed",
                    reason=reason, env=services.env,
                    memory_id=t.get("memory_id"), source_description_id=t.get("source_description_id"))

        try:
            present, before = _present_in(services.reader, a)
            if not present:  # retitled/removed by someone else: never claim success without a delete
                done("rollback-failed", "not found by title on FamilySearch; verify by id manually")
                continue
            if not execute:
                out.append(Outcome(t["key"], kind, who, "would-rollback", "dry run"))
                continue
            try:
                if is_mem:
                    services.memories.delete(oid, reason=ROLLBACK_REASON)
                else:
                    services.sources.delete(oid, reason=ROLLBACK_REASON)
            except Exception as e:
                done("rollback-failed", f"delete error: {_http_status(e)}")
                raise _Stop("delete failed") from e
            present2, after = _present_in(services.reader, a)
            if after != before - 1:
                done("rollback-failed", f"absence read-back mismatch (before {before}, after {after})")
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
    return summary


# ---------------------------------------------------------------- real services

class _SourcesAdapter:
    """Sources writes: create/attach via the verified library; delete via the locked generated API."""

    def __init__(self, sources, discovery, api, allow_production: bool):
        self._sources, self._discovery, self._api = sources, discovery, api
        self._allow_production = allow_production is True

    def create_description(self, about_url, title, citation, *, reason):
        return self._sources.create_description(about_url, title, citation, reason=reason)

    def attach_to_person(self, person_id, source_description_id, *, reason):
        import urllib.parse
        from familysearch._urls import join_path
        parts = urllib.parse.urlsplit(self._discovery.href("source-descriptions"))
        base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        self._sources.attach_to_person(person_id, join_path(base, source_description_id), reason=reason)

    def delete(self, source_description_id, *, reason):
        # The registry lock decides: verified-on-beta, so production needs allow_production.
        self._api.delete_source_description(
            source_description_id, reason=reason, confirm_destructive=True,
            allow_production=self._allow_production)


def build_services(archive: Path, env: str, allow_production: bool) -> ExecServices:
    """Real services from Brave's session. Refuses unless the session belongs to ``env``."""
    from familysearch.api import FamilySearchApi
    from familysearch.auth import Environment, extract_brave_token
    from familysearch.client import FSClient
    from familysearch.discovery import Discovery
    from familysearch.memories import Memories
    from familysearch.sources import Sources

    from .prod_reader import ReadOnlyFS

    environment = {"beta": Environment.BETA, "production": Environment.PRODUCTION}.get(env)
    if environment is None:
        raise GateError(f"unknown environment {env!r}")
    token = extract_brave_token()
    reader = ReadOnlyFS.for_environment(environment, token_fn=lambda: token)  # session must match env
    client = FSClient(token, environment)  # separate client: the reader's is locked read-only
    discovery = Discovery(client)
    return ExecServices(
        reader=reader,
        memories=Memories(client, discovery),
        sources=_SourcesAdapter(Sources(client, discovery), discovery, FamilySearchApi(client),
                                allow_production),
        archive=Path(archive),
        env=env,
    )
