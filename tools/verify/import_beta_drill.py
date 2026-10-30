"""BETA-only end-to-end drill of the import pipeline with fictional ZZSPIKE data.

    python -m tools.verify.import_beta_drill

Creates one deceased throwaway person on beta.familysearch.org, builds a temporary archive
OUTSIDE the repo that links to it, then drives the real ``familysearch_import`` CLI
(links, plan, execute dry run / wrong hash / --execute / repeat, rollback, re-plan), probes
list pagination with 30 extra memories and tears everything down in a ``finally`` block.
Production is never contacted (the session must answer 200 on beta). Tokens and account
ids are never printed: evidence is counts and fictional object ids only; exceptions are
reported by type and HTTP status.

Exit status: 0 only when every step and every teardown action passed.
"""
from __future__ import annotations

import contextlib
import io
import json
import plistlib
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

LINE = "zzline"
DEAD_FOLDER = "1800-1870 Zzspike Drillperson"
LIVING_FOLDER = "1990 Zzspike Livingone"
NOTE_NAMES = ("Zzspike Note A.png", "Zzspike Note B.png", "Zzspike Note C.png")
PAGE_COUNT = 30
REASON = "ZZSPIKE import drill"
ZERO_HASH = "0" * 64
_TOKENISH = re.compile(r"(?i)(bearer\s+\S+|[A-Za-z0-9_\-]{32,})")


# ---------------------------------------------------------------- pure helpers

def scrub(text: str) -> str:
    """Hide anything token-shaped before it is printed."""
    return _TOKENISH.sub("<redacted>", str(text))


def format_step(name: str, ok: bool, evidence: str = "") -> str:
    line = f"{'PASS' if ok else 'FAIL'} {name}"
    return scrub(f"{line}: {evidence}" if evidence else line)


def describe_error(e: BaseException) -> str:
    """Type and HTTP status only (never str(e), which can carry response text)."""
    status = getattr(e, "status", None) or getattr(e, "status_code", None)
    return type(e).__name__ + (f" HTTP {status}" if status else "")


def beta_person_url(pid: str) -> str:
    return f"https://beta.familysearch.org/en/tree/person/details/{pid}"


def write_webloc(path: Path, url: str) -> None:
    path.write_bytes(plistlib.dumps({"URL": url}))


def build_archive(root: Path, pid: str, png: Callable[[], bytes]) -> dict:
    """Lay out the drill archive under ``root``; returns the paths it made."""
    dead = root / LINE / DEAD_FOLDER
    living = root / LINE / LIVING_FOLDER
    dead.mkdir(parents=True)
    living.mkdir(parents=True)
    write_webloc(dead / "Zzspike Drillperson.webloc", beta_person_url(pid))
    notes = []
    for name in NOTE_NAMES:
        (dead / name).write_bytes(png())
        notes.append(dead / name)
    (living / "Zzspike Living.png").write_bytes(png())
    return {"root": root, "dead": dead, "living": living, "notes": notes,
            "person_key": f"{LINE}/{DEAD_FOLDER}"}


def write_approvals(import_dir: Path, plan_hash: str, person_key: str, pid: str) -> Path:
    import_dir.mkdir(parents=True, exist_ok=True)
    path = import_dir / "approvals.json"
    path.write_text(json.dumps({"approved_links": {person_key: pid},
                                "approved_plan_hash": plan_hash}, indent=2) + "\n",
                    encoding="utf-8")
    return path


def read_ledger(path: Path) -> list[dict]:
    if not Path(path).is_file():
        return []
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def count_ledger(records: list[dict], type_: str, status: str | None = None) -> int:
    return sum(1 for r in records if r.get("type") == type_
               and (status is None or r.get("status") == status))


def created_memory_ids(records: list[dict]) -> list[str]:
    return [r["memory_id"] for r in records
            if r.get("type") == "result" and r.get("status") == "created" and r.get("memory_id")]


def plan_stats(plan: dict) -> dict:
    """Counts the drill asserts on, from a Plan.json document."""
    planned = already = 0
    living_planned = 0
    for p in plan.get("people", []):
        for a in p.get("actions", []):
            is_upload = a.get("kind") == "upload-document"
            if is_upload and a.get("state") == "planned":
                planned += 1
                if str(p.get("folder", "")).startswith("1990 "):
                    living_planned += 1
            if is_upload and a.get("state") == "already-present":
                already += 1
    return {"planned_uploads": planned, "already_present": already, "living_planned": living_planned,
            "env": plan.get("env"), "hash": plan.get("plan_hash")}


_WROTE = re.compile(r"wrote (\S+) and \S+ \(plan hash ([0-9a-f]{64})\)")


def parse_plan_output(out: str) -> tuple[str, str] | None:
    """(plan file path, plan hash) from the ``plan`` command's output."""
    m = _WROTE.search(out)
    return (m.group(1), m.group(2)) if m else None


def page_report(urls: list[str]) -> str:
    """How a list read paged: request count and each follow-up's path and query keys."""
    if not urls:
        return "no list requests seen"
    followups = []
    for u in urls[1:]:
        path, _, query = u.partition("?")
        keys = sorted({kv.split("=")[0] for kv in query.split("&") if kv})
        followups.append(path.rsplit("/", 2)[-1] + ("?" + ",".join(keys) if keys else ""))
    nxt = "links.next followed" if followups else "single page (links.next not needed or absent)"
    return f"{len(urls)} list request(s); {nxt}; follow-ups: {followups or 'none'}"


def poll(fn: Callable[[], object], ok: Callable[[object], bool], *, tries: int = 12,
         delay: float = 3.0, sleep: Callable[[float], None] = time.sleep):
    """Value of fn() once ok(value), else the last value (eventual consistency)."""
    value = fn()
    for _ in range(tries - 1):
        if ok(value):
            return value
        sleep(delay)
        value = fn()
    return value


class Report:
    def __init__(self, out: Callable[[str], None] = print):
        self._out = out
        self.failures: list[str] = []

    def step(self, name: str, ok: bool, evidence: str = "") -> bool:
        self._out(format_step(name, ok, evidence))
        if not ok:
            self.failures.append(name)
        return ok

    def summary(self) -> str:
        if self.failures:
            return f"DRILL FAILED: {len(self.failures)} problem(s): {'; '.join(self.failures)}"
        return "DRILL PASSED: every step and teardown succeeded"

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


# ---------------------------------------------------------------- live pieces

def run_cli(argv: list[str]) -> tuple[int, str]:
    """Run the import CLI in-process; (exit code, stdout+stderr)."""
    from familysearch_import.__main__ import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = main(argv)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
    return rc, buf.getvalue()


def summary_lines(out: str) -> str:
    keep = [ln for ln in out.splitlines() if ln.startswith(("summary:", "STOPPED", "error:", "EXECUTED",
                                                            "DRY RUN", "ROLLED BACK"))]
    return " | ".join(keep) or "(no summary)"


class Drill:
    def __init__(self, report: Report):
        from familysearch.discovery import Discovery
        from familysearch.memories import Memories
        from familysearch.tree import TreeWriter
        from familysearch_import.prod_reader import ReadOnlyFS

        from .runner import build_beta_client

        self.r = report
        self.client = build_beta_client()  # refuses unless the session is BETA
        discovery = Discovery(self.client)
        self.writer = TreeWriter(self.client, discovery)
        self.mem = Memories(self.client, discovery)
        self.reader = ReadOnlyFS.beta()
        self.list_urls: list[str] = []
        self._spy_reader()
        self.pid: str | None = None
        self.root: Path | None = None
        self.arch: dict = {}
        self.plan_hash: str | None = None

    def _spy_reader(self) -> None:
        inner = self.reader._client.request

        def spy(method, path_or_url, *a, **kw):
            url = str(path_or_url)
            if "artifacts" in url or "memories" in url:
                self.list_urls.append(url.split("://", 1)[-1].partition("/")[2])
            return inner(method, path_or_url, *a, **kw)

        self.reader._client.request = spy  # type: ignore[method-assign]

    # -- helpers
    @property
    def import_dir(self) -> Path:
        return self.root / "_import"

    @property
    def ledger_path(self) -> Path:
        return self.import_dir / "ledger.jsonl"

    def ledger(self) -> list[dict]:
        return read_ledger(self.ledger_path)

    def memory_ids(self) -> list[str]:
        return list(self.reader.get_memory_ids(self.pid))

    def cli(self, *args: str) -> tuple[int, str]:
        return run_cli([args[0], "--archive", str(self.root), *args[1:]])

    def execute_args(self, plan_name: str, confirm: str, execute: bool) -> tuple[str, ...]:
        extra = ("--execute",) if execute else ()
        return ("execute", "--env", "beta", "--plan", plan_name, "--confirm-hash", confirm, *extra)

    # -- steps
    def step1_setup(self) -> bool:
        from familysearch.models import FACT_BIRTH, FACT_DEATH, build_fact, build_person

        from .fixtures import unique_png

        person = build_person(given="Zzspike", surname="Drillperson", gender="Male", living=False,
                              facts=[build_fact(FACT_BIRTH, date="1800")])
        self.pid = self.writer.create_person(person, reason=REASON)
        self.writer.add_fact(self.pid, build_fact(FACT_DEATH, date="1870"), reason=REASON)
        self.root = Path(tempfile.mkdtemp(prefix="zzspike-drill-"))
        self.arch = build_archive(self.root, self.pid, unique_png)
        return self.r.step("1 setup", True, f"person {self.pid} on beta; archive {self.root.name} "
                           f"(1 dead folder, 3 PNGs, 1 living folder)")

    def step2_links_and_plan(self) -> bool:
        rc, out = self.cli("links", "--env", "beta")
        links = json.loads((self.import_dir / "links.json").read_text(encoding="utf-8")) \
            if rc == 0 else {}
        verdicts = {f"{p['line']}/{p['folder']}": p.get("verdict") for p in links.get("people", [])}
        if not self.r.step("2a links", rc == 0 and verdicts.get(self.arch["person_key"]) == "confirmed",
                           f"rc={rc}; verdict={verdicts.get(self.arch['person_key'])}"):
            return False
        rc, out = self.cli("plan", "--env", "beta")
        parsed = parse_plan_output(out)
        if rc != 0 or not parsed:
            return self.r.step("2b plan", False, f"rc={rc}; {summary_lines(out)}")
        self.plan_name = Path(parsed[0]).name
        stats = plan_stats(json.loads(Path(parsed[0]).read_text(encoding="utf-8")))
        self.plan_hash = parsed[1]
        ok = (stats["planned_uploads"] == 3 and stats["living_planned"] == 0
              and stats["env"] == "beta" and stats["hash"] == self.plan_hash)
        return self.r.step("2b plan", ok, f"{stats['planned_uploads']} planned uploads, "
                           f"{stats['living_planned']} for the living folder, env={stats['env']}")

    def step3_execute(self) -> bool:
        write_approvals(self.import_dir, self.plan_hash, self.arch["person_key"], self.pid)
        rc, out = self.cli(*self.execute_args(self.plan_name, self.plan_hash, False))
        n, intents = len(self.memory_ids()), count_ledger(self.ledger(), "intent")
        ok = self.r.step("3a dry run", rc == 0 and n == 0 and intents == 0,
                         f"rc={rc}; memories={n}; ledger intents={intents}")
        rc, out = self.cli(*self.execute_args(self.plan_name, ZERO_HASH, True))
        intents = count_ledger(self.ledger(), "intent")
        ok &= self.r.step("3b wrong hash refused", rc == 2 and intents == 0,
                          f"rc={rc}; ledger intents={intents}; {summary_lines(out)}")
        rc, out = self.cli(*self.execute_args(self.plan_name, self.plan_hash, True))
        ids = poll(self.memory_ids, lambda v: len(v) == 3)
        recs = self.ledger()
        made = created_memory_ids(recs)
        self.created = made
        ok &= self.r.step("3c execute", rc == 0 and len(ids) == 3 and len(made) == 3
                          and set(made) <= set(ids),
                          f"rc={rc}; memories on FS={len(ids)}; ledger created with memory id={len(made)}")
        before = (len(recs), sorted(self.memory_ids()))
        rc, out = self.cli(*self.execute_args(self.plan_name, self.plan_hash, True))
        after = (len(self.ledger()), sorted(self.memory_ids()))
        ok &= self.r.step("3d repeat execute is a no-op", rc == 0 and before == after,
                          f"rc={rc}; ledger lines {before[0]}->{after[0]}; memories {len(before[1])}->{len(after[1])}")
        return bool(ok)

    def step4_rollback(self) -> bool:
        rc, out = self.cli("rollback", "--env", "beta")
        still = len(self.memory_ids())
        ok = self.r.step("4a rollback dry run", rc == 0 and still == 3, f"rc={rc}; memories={still}")
        rc, out = self.cli("rollback", "--env", "beta", "--execute")
        left = poll(self.memory_ids, lambda v: not v)
        gone = [m for m in self.created if m not in left]
        rolled = count_ledger(self.ledger(), "rollback", "rolled-back")
        ok &= self.r.step("4b rollback", rc == 0 and len(gone) == 3 and rolled == 3,
                          f"rc={rc}; memories gone by id={len(gone)}/3; ledger rolled-back lines={rolled}")
        rc, out = self.cli("plan", "--env", "beta")
        parsed = parse_plan_output(out)
        if rc != 0 or not parsed:
            return self.r.step("4c re-plan", False, f"rc={rc}; {summary_lines(out)}") and False
        stats = plan_stats(json.loads(Path(parsed[0]).read_text(encoding="utf-8")))
        ok &= self.r.step("4c re-plan keeps rolled-back keys executed",
                          stats["planned_uploads"] == 0 and stats["already_present"] == 3,
                          f"planned={stats['planned_uploads']}; already-present(in ledger)={stats['already_present']}")
        write_approvals(self.import_dir, parsed[1], self.arch["person_key"], self.pid)
        rc, out = self.cli(*self.execute_args(Path(parsed[0]).name, parsed[1], True))
        n = len(self.memory_ids())
        ok &= self.r.step("4d re-execute uploads nothing", rc == 0 and n == 0,
                          f"rc={rc}; memories={n}; {summary_lines(out)}")
        return bool(ok)

    def step5_pagination(self) -> bool:
        from .fixtures import unique_png

        titles = [f"Zzspike Page {i:02d}" for i in range(1, PAGE_COUNT + 1)]
        self.page_ids: list[str] = []
        for t in titles:
            mid = self.mem.upload_to_person(self.pid, unique_png(), filename=f"{t}.png",
                                            content_type="image/png", title=t, reason=REASON)
            self.page_ids.append(mid)
        self.list_urls.clear()
        got = poll(lambda: self.reader.get_memory_titles(self.pid), lambda v: len(v) >= PAGE_COUNT)
        report = page_report(self.list_urls)
        ok = sorted(got) == sorted(titles)
        for mid in self.page_ids:
            self.mem.delete(mid, reason=REASON)
        left = poll(self.memory_ids, lambda v: not v)
        ok_del = not left
        return self.r.step("5 pagination probe", ok and ok_del,
                           f"uploaded {PAGE_COUNT}, listed {len(got)}; {report}; after delete {len(left)} left")

    # -- teardown
    def teardown(self) -> None:
        if self.pid:
            try:
                for mid in self.memory_ids():
                    self.mem.delete(mid, reason=REASON)
                left = poll(self.memory_ids, lambda v: not v)
                self.r.step("6a teardown memories", not left, f"{len(left)} left")
            except Exception as e:  # noqa: BLE001
                self.r.step("6a teardown memories", False, describe_error(e))
            try:
                self.writer.delete_person(self.pid, reason=REASON)
                st = poll(lambda: self.reader.get_fs_person(self.pid).status,
                          lambda v: v in ("deleted", "not-found"))
                self.r.step("6b teardown person", st in ("deleted", "not-found"), f"status={st}")
            except Exception as e:  # noqa: BLE001
                self.r.step("6b teardown person", False, describe_error(e))
        if self.root:
            try:
                shutil.rmtree(self.root)
                self.r.step("6c teardown archive", not self.root.exists())
            except OSError as e:
                self.r.step("6c teardown archive", False, describe_error(e))


def run(report: Report | None = None) -> int:
    report = report or Report()
    from familysearch.errors import FSError

    try:
        drill = Drill(report)  # step 0: refuses unless the Brave session is beta
    except FSError as e:
        report.step("0 beta session", False, describe_error(e))
        print(report.summary())
        return 2
    report.step("0 beta session", True, "session answers on beta")
    try:
        for step in (drill.step1_setup, drill.step2_links_and_plan, drill.step3_execute,
                     drill.step4_rollback, drill.step5_pagination):
            if not step():
                break
    except Exception as e:  # noqa: BLE001 - a crash is a failed step, teardown still runs
        report.step(f"crash in {getattr(step, '__name__', 'step')}", False, describe_error(e))
    finally:
        drill.teardown()
    print(report.summary())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(run())
