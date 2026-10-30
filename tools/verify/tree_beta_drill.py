"""BETA-only end-to-end drill of the tree executor with invented, deceased-style people.

    python -m tools.verify.tree_beta_drill

Creates four throwaway people on beta.familysearch.org (two parents, a child, an unrelated
control), builds a scratch archive in a temp dir OUTSIDE the repo (links.json, approvals.json,
relationships.json) and drives the real ``familysearch_import`` CLI: tree-plan, tree-execute
(dry run, wrong hash, one action at a time, repeat), a second plan whose create-person now has a
plausible match, and tree-rollback. It then records two observed API facts and tears everything
down in a ``finally`` block. Production is never contacted (the session must answer 200 on beta).
Tokens and account ids are never printed: evidence is counts and invented object ids only;
exceptions are reported by type and HTTP status.

Exit status: 0 only when every step and every teardown action passed.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from familysearch_import.treeplan import NO_PARENTS_NOTE

from .import_beta_drill import (Report, describe_error, poll, read_ledger, run_cli, scrub,
                                summary_lines)

LINE = "drillline"
REASON = "Drill: tree executor beta test"
ZERO_HASH = "0" * 64
GONE = ("deleted", "not-found")
CREATED_FIELDS = ("couple_id", "child_parents_id", "person_id")

# name, gender, birth, death
PEOPLE = {
    "p1": ("Drillfirst Drilltestname", "Male", 1850, 1920),
    "p2": ("Drillsecond Drilltestname", "Female", 1852, 1922),
    "child": ("Drillthird Drilltestname", "Male", 1880, 1950),
    "control": ("Drillcontrol Drilltestname", "Female", 1860, 1930),
}
NEW_PERSON = {"label": "Drillfifth Drillcreated", "birth": 1855, "death": 1925}
_PLAN_HASH = re.compile(r"wrote (\S+) and .*\(plan hash ([0-9a-f]{64})\)")


# ---------------------------------------------------------------- pure helpers

def folder_of(name: str, birth: int, death: int) -> str:
    return f"{birth}-{death} {name}"


def parse_tree_plan_output(out: str) -> tuple[str, str] | None:
    m = _PLAN_HASH.search(out)
    return (m.group(1), m.group(2)) if m else None


def write_inputs(import_dir: Path, *, ids: dict[str, str], with_people: bool = True) -> dict:
    """Write links.json, relationships.json and approvals.json for the scratch archive.

    With ``with_people`` the edges are: p1+p2 spouse, p1->child and p2->child parent-child; the
    create candidate is always present. Returns the approved keys.
    """
    import_dir.mkdir(parents=True, exist_ok=True)
    nm = {k: v[0] for k, v in PEOPLE.items()}
    people, edges, approved_rel = [], [], []
    if with_people:
        for k, (name, _g, b, d) in PEOPLE.items():
            people.append({"line": LINE, "folder": folder_of(name, b, d), "verdict": "confirmed",
                           "chosen_id": ids[k]})
            (import_dir.parent / LINE / folder_of(name, b, d)).mkdir(parents=True, exist_ok=True)  # archive side
        fk = {k: f"{LINE}/{folder_of(v[0], v[2], v[3])}" for k, v in PEOPLE.items()}
        x, y = sorted((ids["p1"], ids["p2"]))
        spouse_key = f"spouse:{x}:{y}"
        edges.append({"key": spouse_key, "kind": "spouse", "a": nm["p1"], "b": nm["p2"],
                      "a_fs": ids["p1"], "b_fs": ids["p2"], "status": "missing-in-familysearch", "note": "",
                      "a_folder": fk["p1"], "b_folder": fk["p2"]})
        approved_rel.append(spouse_key)
        for par in ("p1", "p2"):
            k = f"parent-child:{ids[par]}:{ids['child']}"
            edges.append({"key": k, "kind": "parent-child", "a": nm[par], "b": nm["child"],
                          "a_fs": ids[par], "b_fs": ids["child"], "status": "missing-in-familysearch",
                          "note": NO_PARENTS_NOTE, "a_folder": fk[par], "b_folder": fk["child"]})
            approved_rel.append(k)
    cand = dict(NEW_PERSON)
    (import_dir / "links.json").write_text(json.dumps({"env": "beta", "people": people}, indent=2) + "\n",
                                           encoding="utf-8")
    (import_dir / "relationships.json").write_text(
        json.dumps({"edges": edges, "create_candidates": [cand], "env": "beta"}, indent=2) + "\n", encoding="utf-8")
    write_approvals(import_dir, approved_rel, [cand["label"]], None)
    return {"approved_relationships": approved_rel}


def write_approvals(import_dir: Path, rels: list[str], creations: list[str], plan_hash: str | None) -> Path:
    data: dict = {"approved_relationships": rels, "approved_creations": creations}
    if plan_hash:
        data["approved_tree_plan_hash"] = plan_hash
    path = Path(import_dir) / "approvals.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def created_by_key(records: list[dict]) -> dict[str, dict]:
    """Ledger ``result`` lines with status created: action key -> created id dict."""
    return {r["key"]: dict(r["created"]) for r in records
            if r.get("type") == "result" and r.get("status") == "created"
            and isinstance(r.get("created"), dict) and r["created"]}


def all_created_ids(records: list[dict]) -> dict[str, list[str]]:
    """Every id the tree ledger ever recorded (result or progress), by field, for teardown."""
    out: dict[str, list[str]] = {f: [] for f in CREATED_FIELDS}
    for r in records:
        if r.get("type") in ("result", "progress") and isinstance(r.get("created"), dict):
            for f in CREATED_FIELDS:
                v = r["created"].get(f)
                if isinstance(v, str) and v not in out[f]:
                    out[f].append(v)
    return out


def count_outcomes(out: str, status: str) -> int:
    return sum(1 for ln in out.splitlines() if ln.startswith(status + ":"))


def is_gone_error(e: BaseException) -> bool:
    return (getattr(e, "status", None) or getattr(e, "status_code", None)) in (404, 410)


# ---------------------------------------------------------------- live drill

class Drill:
    def __init__(self, report: Report):
        from familysearch.discovery import Discovery
        from familysearch.tree import TreeWriter
        from familysearch_import.prod_reader import ReadOnlyFS

        from .runner import build_beta_client

        self.r = report
        self.client = build_beta_client()  # refuses unless the session is BETA
        self.writer = TreeWriter(self.client, Discovery(self.client))
        self.reader = ReadOnlyFS.beta()
        self.ids: dict[str, str] = {}
        self.root: Path | None = None
        self.root2: Path | None = None
        self.root3: Path | None = None
        self.keys: dict[str, str] = {}
        self.plan_name = ""
        self.plan_hash = ""
        self.extra_rels: list[tuple[str, str]] = []  # (kind, id) created directly by the drill
        self.observed: list[str] = []
        self.new_id = ""

    # -- helpers
    @property
    def import_dir(self) -> Path:
        return self.root / "_import"

    def ledger(self) -> list[dict]:
        return read_ledger(self.import_dir / "tree-ledger.jsonl")

    def cli(self, cmd: str, *args: str, root: Path | None = None) -> tuple[int, str]:
        return run_cli([cmd, "--archive", str(root or self.root), *args])

    def exec_args(self, plan_name: str, confirm: str, *, execute: bool, only: tuple[str, ...] = ()) -> tuple:
        extra = [x for k in only for x in ("--only", k)] + (["--execute"] if execute else [])
        return ("--env", "beta", "--plan", plan_name, "--confirm-hash", confirm, *extra)

    def rels(self, pid: str) -> set[tuple[str, str]]:
        return {(r.relationship, r.fs.id) for r in self.reader.get_relatives(pid)}

    def status(self, pid: str) -> str:
        return self.reader.get_fs_person(pid).status

    def has(self, a: str, rel: str, b: str) -> bool:
        return (rel, b) in self.rels(a)

    # -- step 1
    def step1_setup(self) -> bool:
        from familysearch.models import FACT_BIRTH, FACT_DEATH, build_fact, build_person

        self.root = Path(tempfile.mkdtemp(prefix="tree-drill-"))
        for k, (name, gender, b, d) in PEOPLE.items():
            given, surname = name.split(" ", 1)
            person = build_person(given=given, surname=surname, gender=gender, living=False,
                                  facts=[build_fact(FACT_BIRTH, date=str(b))])
            self.ids[k] = self.writer.create_person(person, reason=REASON)
            self.writer.add_fact(self.ids[k], build_fact(FACT_DEATH, date=str(d)), reason=REASON)
        okc = all(self.status(i) == "ok" for i in self.ids.values())
        write_inputs(self.import_dir, ids=self.ids)
        return self.r.step("1 setup", okc and len(set(self.ids.values())) == 4,
                           f"4 invented persons {sorted(self.ids.values())}; scratch archive {self.root.name}")

    # -- step 2
    def step2_plan(self) -> bool:
        rc, out = self.cli("tree-plan", "--env", "beta")
        parsed = parse_tree_plan_output(out)
        if rc != 0 or not parsed:
            return self.r.step("2 tree-plan", False, f"rc={rc}; {scrub(out.strip()[:200])}")
        data = json.loads(Path(parsed[0]).read_text(encoding="utf-8"))
        planned = {a["kind"]: a["key"] for a in data["actions"] if a["state"] == "planned"}
        planned_n = sum(1 for a in data["actions"] if a["state"] == "planned")
        self.plan_name, self.plan_hash = Path(parsed[0]).name, parsed[1]
        self.keys = {"couple": planned.get("add-couple", ""), "cp": planned.get("add-child-parents", ""),
                     "person": planned.get("create-person", "")}
        ok = planned_n == 3 and len(planned) == 3 and data["env"] == "beta" and data["plan_hash"] == parsed[1]
        write_approvals(self.import_dir, json.loads((self.import_dir / "approvals.json").read_text(
            encoding="utf-8"))["approved_relationships"], [NEW_PERSON["label"]], self.plan_hash)
        return self.r.step("2 tree-plan", ok, f"{planned_n} planned: {sorted(planned)}; env={data['env']}")

    # -- steps 3 and 4
    def step3_dry_run(self) -> bool:
        rc, out = self.cli("tree-execute", *self.exec_args(self.plan_name, self.plan_hash, execute=False))
        i = self.ids
        untouched = (not self.rels(i["p1"]) and not self.rels(i["p2"]) and not self.rels(i["child"]))
        intents = sum(1 for r in self.ledger() if r.get("type") == "intent")
        return self.r.step("3 dry run writes nothing",
                           rc == 0 and count_outcomes(out, "would-create") == 3 and untouched and intents == 0,
                           f"rc={rc}; would-create={count_outcomes(out, 'would-create')}; "
                           f"setup people still unrelated={untouched}; ledger intents={intents}")

    def step3b_not_a_couple(self) -> bool:
        """A two-parent child link whose parents are NOT a couple on FamilySearch must be skipped.

        The plan is built from a scratch relationships.json that (falsely) says the parents are already a
        couple, so the planner lets the action through; the executor's fresh read must refuse it. Dry run only.
        """
        i = self.ids
        self.root3 = Path(tempfile.mkdtemp(prefix="tree-drill3-"))
        imp = self.root3 / "_import"
        imp.mkdir(parents=True)
        spec = {"p1": PEOPLE["p1"], "control": PEOPLE["control"], "child": PEOPLE["child"]}
        fkey, people = {}, []
        for k, (name, _g, b, d) in spec.items():
            (self.root3 / LINE / folder_of(name, b, d)).mkdir(parents=True, exist_ok=True)
            people.append({"line": LINE, "folder": folder_of(name, b, d), "verdict": "confirmed",
                           "chosen_id": i[k]})
            fkey[k] = f"{LINE}/{folder_of(name, b, d)}"
        (imp / "links.json").write_text(json.dumps({"env": "beta", "people": people}), encoding="utf-8")
        x, y = sorted((i["p1"], i["control"]))
        edges = [{"key": f"spouse:{x}:{y}", "kind": "spouse", "a": "A", "b": "B", "a_fs": i["p1"],
                  "b_fs": i["control"], "status": "present", "note": "", "a_folder": fkey["p1"],
                  "b_folder": fkey["control"]}]
        keys = []
        for par in ("p1", "control"):
            keys.append(f"parent-child:{i[par]}:{i['child']}")
            edges.append({"key": keys[-1], "kind": "parent-child", "a": "P", "b": "C", "a_fs": i[par],
                          "b_fs": i["child"], "status": "missing-in-familysearch", "note": NO_PARENTS_NOTE,
                          "a_folder": fkey[par], "b_folder": fkey["child"]})
        (imp / "relationships.json").write_text(
            json.dumps({"edges": edges, "create_candidates": [], "env": "beta"}), encoding="utf-8")
        write_approvals(imp, keys, [], None)
        rc, out = self.cli("tree-plan", "--env", "beta", root=self.root3)
        parsed = parse_tree_plan_output(out)
        if rc != 0 or not parsed:
            return self.r.step("3b two-parent link, parents not a couple, is skipped", False, f"plan rc={rc}")
        write_approvals(imp, keys, [], parsed[1])
        rc, out = self.cli("tree-execute", *self.exec_args(Path(parsed[0]).name, parsed[1], execute=False),
                           root=self.root3)
        skipped = "not yet a couple" in out and count_outcomes(out, "skipped") == 1
        return self.r.step("3b two-parent link, parents not a couple, is skipped",
                           rc == 0 and skipped and count_outcomes(out, "would-create") == 0,
                           f"rc={rc}; skipped with 'not yet a couple' reason={skipped}; "
                           f"would-create={count_outcomes(out, 'would-create')}")

    def step4_wrong_hash(self) -> bool:
        rc, out = self.cli("tree-execute", *self.exec_args(self.plan_name, ZERO_HASH, execute=True))
        intents = sum(1 for r in self.ledger() if r.get("type") == "intent")
        untouched = not self.rels(self.ids["p1"])
        return self.r.step("4 wrong confirm-hash refused", rc == 2 and intents == 0 and untouched,
                           f"rc={rc}; ledger intents={intents}; {summary_lines(out)}")

    # -- step 5
    def _run_one(self, which: str, field: str) -> tuple[bool, str]:
        rc, out = self.cli("tree-execute", *self.exec_args(self.plan_name, self.plan_hash, execute=True,
                                                          only=(self.keys[which],)))
        made = created_by_key(self.ledger()).get(self.keys[which], {})
        return rc == 0 and bool(made.get(field)) and count_outcomes(out, "created") == 1, \
            f"rc={rc}; ledger {field}={'yes' if made.get(field) else 'no'}; {summary_lines(out)}"

    def step5_execute(self) -> bool:
        i = self.ids
        ok = True
        good, ev = self._run_one("couple", "couple_id")
        seen = poll(lambda: self.has(i["p1"], "spouse", i["p2"]) and self.has(i["p2"], "spouse", i["p1"]),
                    lambda v: v)
        ctl = not ({i["p1"], i["p2"]} & {rid for _, rid in self.rels(i["control"])})
        ok &= self.r.step("5a add-couple", good and bool(seen) and ctl, f"{ev}; spouses both ways={bool(seen)}; "
                          f"control untouched={ctl}")
        good, ev = self._run_one("cp", "child_parents_id")
        parents = poll(lambda: {rid for rel, rid in self.rels(i["child"]) if rel == "parent"},
                       lambda v: v == {i["p1"], i["p2"]})
        lists_child = all(self.has(i[p], "child", i["child"]) for p in ("p1", "p2"))
        ctl = not ({i["p1"], i["p2"], i["child"]} & {rid for _, rid in self.rels(i["control"])})
        ok &= self.r.step("5b add-child-parents", good and parents == {i["p1"], i["p2"]} and lists_child and ctl,
                          f"{ev}; child parents={len(parents)} (expect 2); parents list child={lists_child}; "
                          f"control untouched={ctl}")
        good, ev = self._run_one("person", "person_id")
        new_id = created_by_key(self.ledger()).get(self.keys["person"], {}).get("person_id", "")
        fs = poll(lambda: self.reader.get_fs_person(new_id), lambda v: v.status == "ok") if new_id else None
        person_ok = bool(fs) and fs.status == "ok" and fs.living is False \
            and "drillfifth" in fs.name.lower() and not self.rels(new_id)
        ok &= self.r.step("5c create-person", good and person_ok,
                          f"{ev}; readable={bool(fs) and fs.status == 'ok'}; living={getattr(fs, 'living', None)}; "
                          f"no relationships={bool(new_id) and not self.rels(new_id)}")
        self.new_id = new_id
        self.capture_shapes()
        return bool(ok)

    # -- step 6 and 7
    def step6_rerun(self) -> bool:
        before = (len(self.ledger()), {p: sorted(self.rels(i)) for p, i in self.ids.items()})
        rc, out = self.cli("tree-execute", *self.exec_args(self.plan_name, self.plan_hash, execute=True))
        after = (len(self.ledger()), {p: sorted(self.rels(i)) for p, i in self.ids.items()})
        return self.r.step("6 rerun is a no-op", rc == 0 and before == after and count_outcomes(out, "created") == 0,
                           f"rc={rc}; ledger lines {before[0]}->{after[0]}; created={count_outcomes(out, 'created')}; "
                           f"relationships unchanged={before[1] == after[1]}")

    def step7_name_exists(self) -> bool:
        """A fresh scratch archive (own ledger) approves a create-person for the name that now exists."""
        self.root2 = Path(tempfile.mkdtemp(prefix="tree-drill2-"))
        imp = self.root2 / "_import"
        write_inputs(imp, ids={}, with_people=False)
        rc, out = self.cli("tree-plan", "--env", "beta", root=self.root2)
        parsed = parse_tree_plan_output(out)
        if rc != 0 or not parsed:
            return self.r.step("7 existing name refused", False, f"plan rc={rc}")
        write_approvals(imp, [], [NEW_PERSON["label"]], parsed[1])
        name = Path(parsed[0]).name
        # Dry run only (never executes a create that could duplicate); search indexing may lag, so poll.
        result = poll(lambda: self.cli("tree-execute", *self.exec_args(name, parsed[1], execute=False),
                                       root=self.root2),
                      lambda v: v[0] == 0 and count_outcomes(v[1], "skipped") == 1, tries=10, delay=6.0)
        rc, out = result
        skipped = count_outcomes(out, "skipped")
        match = "plausible match" in out
        return self.r.step("7 create-person on an existing name refused", rc == 0 and skipped == 1 and match
                           and count_outcomes(out, "would-create") == 0,
                           f"rc={rc}; skipped={skipped}; plausible match reason={match}; "
                           f"would-create={count_outcomes(out, 'would-create')}")

    # -- step 8
    def step8_rollback(self) -> bool:
        i = self.ids
        rc, out = self.cli("tree-rollback", "--env", "beta")
        still = self.has(i["p1"], "spouse", i["p2"]) and bool(self.rels(i["child"]))
        ok = self.r.step("8a rollback dry run", rc == 0 and count_outcomes(out, "would-rollback") == 3 and still,
                         f"rc={rc}; would-rollback={count_outcomes(out, 'would-rollback')}; still present={still}; "
                         f"{summary_lines(out)}")
        rc, out = self.cli("tree-rollback", "--env", "beta", "--execute")
        gone_rels = poll(lambda: not self.has(i["p1"], "spouse", i["p2"])
                         and not any(rel == "parent" for rel, _ in self.rels(i["child"])), lambda v: v)
        st = poll(lambda: self.status(self.new_id), lambda v: v in GONE) if self.new_id else "no-id"
        rolled = sum(1 for r in self.ledger() if r.get("type") == "rollback" and r.get("status") == "rolled-back")
        ok &= self.r.step("8b rollback everything", rc == 0 and bool(gone_rels) and st in GONE and rolled == 3,
                          f"rc={rc}; couple and child-parents gone={bool(gone_rels)}; created person status={st}; "
                          f"ledger rolled-back lines={rolled}; {summary_lines(out)}")
        return bool(ok)

    # -- step 10: observed API facts
    def observe(self) -> None:
        i = self.ids
        made = created_by_key(self.ledger()).get(self.keys.get("person", ""), {})
        recs = [r for r in self.ledger() if r.get("type") == "result" and r.get("key") == self.keys.get("person")]
        self.observed.append(
            "create-person with living=false, gender Unknown and year-only Birth/Death facts: "
            + ("ACCEPTED (id returned, read back as deceased)" if made.get("person_id")
               else f"NOT ACCEPTED or not run ({recs[-1].get('status') if recs else 'no result'}: "
                    f"{recs[-1].get('reason') if recs else ''})"))
        try:
            cp = self.writer.create_parent_child(child_id=i["child"], parent_ids=[i["p1"], i["p2"]], reason=REASON)
            self.extra_rels.append(("child_parents_id", cp))
            derived = poll(lambda: self.has(i["p1"], "spouse", i["p2"]), lambda v: v, tries=6)
            self.writer.delete_child_and_parents(cp, reason=REASON)
            parents_gone = poll(lambda: not any(rel == "parent" for rel, _ in self.rels(i["child"])), lambda v: v)
            left = poll(lambda: self.has(i["p1"], "spouse", i["p2"]), lambda v: not v, tries=6)
            self.observed.append(
                f"two-parent child link: derived couple appeared after the link={bool(derived)}; "
                f"child-and-parents deleted={bool(parents_gone)}; "
                f"derived couple STILL PRESENT after deleting only the child-and-parents={bool(left)}")
            if left:
                cid = self.find_couple_id(i["p1"], i["p2"])
                if cid:
                    self.extra_rels.append(("couple_id", cid))
                    self.writer.delete_couple(cid, reason=REASON)
                    gone = poll(lambda: not self.has(i["p1"], "spouse", i["p2"]), lambda v: v, tries=6)
                    self.observed.append(f"derived couple cleaned up by id: {bool(gone)}")
                else:
                    self.observed.append("derived couple id not found; person deletion in teardown removes it")
        except Exception as e:  # noqa: BLE001
            self.observed.append(f"derived-couple observation did not complete: {describe_error(e)}")

    def capture_shapes(self) -> None:
        """Record observed API response shapes (booleans and counts only; never ids or names)."""
        i = self.ids
        obs = self.observed.append
        try:
            self.reader.get_relatives(i["control"], strict=True)
            obs("strict section links present for a person with no relatives (control): yes")
        except Exception as e:  # noqa: BLE001
            obs(f"strict section links for a person with no relatives (control): NO - {describe_error(e)}")
        if self.new_id:
            try:
                self.reader.get_relatives(self.new_id, strict=True)
                obs("strict section links present for the freshly created person: yes")
            except Exception as e:  # noqa: BLE001
                obs(f"strict section links for the freshly created person: NO - {describe_error(e)}")
        cid = created_by_key(self.ledger()).get(self.keys.get("couple", ""), {}).get("couple_id")
        if cid:
            try:
                from familysearch.discovery import Discovery
                from familysearch.tree import TreeReader

                disc = Discovery(self.client)
                rels = TreeReader(self.client, disc)._fetch_all(disc.person_links(i["p1"]), "spouses",
                                                                "relationships")
                obs("spouses section carries relationships with id/person1/person2: "
                    + str(bool(rels) and all(r.get("id") and r.get("person1") and r.get("person2") for r in rels)))
                obs(f"get_couple(id) names the pair: {self.reader.get_couple(cid) == frozenset((i['p1'], i['p2']))}; "
                    f"get_couple_ids == [id]: {self.reader.get_couple_ids(i['p1'], i['p2']) == [cid]}")
                from familysearch.tree import couple_url

                raw = self.reader._get_json(couple_url(self.client.base_url, cid), "couple") or {}
                rel0 = (raw.get("relationships") or [{}])[0]
                obs(f"couple GET top-level keys: {sorted(raw)}; relationship keys: {sorted(rel0)}")
                ex = self.reader.get_couple_extras(cid)
                me = self.reader.current_user_ids()
                user = (self.reader._get_json(self.client.base_url + "/platform/users/current", "user") or {}) \
                    .get("users", [{}])[0]
                contributors = {w for w, _ in ex["changes"]}
                obs("which /users/current fields appear as change-history contributor resourceIds: "
                    + ", ".join(f"{f}={bool(user.get(f)) and str(user.get(f)) in contributors}"
                                for f in ("id", "personId", "treeUserId", "cisUserId", "contributorId"))
                    + f"; user-record keys present: {sorted(user)}")
                who = [w for w, _ in ex["changes"]]
                obs(f"couple change history: entries={len(who)}; contributor populated={bool(who) and all(who)}; "
                    f"all contributors are the current user={bool(who) and all(w in me for w in who)}; "
                    f"facts={ex['facts']} sources={ex['sources']} notes={ex['notes']}")
            except Exception as e:  # noqa: BLE001
                obs(f"couple shape observation did not complete: {describe_error(e)}")
        if self.new_id:
            try:
                ch = self.reader.get_change_summary(self.new_id)
                me = self.reader.current_user_ids()
                obs(f"person change history: entries={len(ch)}; contributor populated="
                    f"{bool(ch) and all(w for w, _ in ch)}; all contributors are the current user="
                    f"{bool(ch) and all(w in me for w, _ in ch)}")
            except Exception as e:  # noqa: BLE001
                obs(f"person change-history observation did not complete: {describe_error(e)}")
        try:
            hits = self.reader.search_duplicates("Zzqxnonexistent", "Zzqxnotreal", 1700, None)
            obs(f"duplicate search for a nonsense name parsed strictly; hits={len(hits)}")
        except Exception as e:  # noqa: BLE001
            obs(f"duplicate search for a nonsense name FAILED strict parsing (real feed shape differs): "
                f"{describe_error(e)}")

    def find_couple_id(self, a: str, b: str) -> str | None:
        """Best effort: the couple relationship id between a and b from the person's spouse relationships."""
        try:
            from familysearch.tree import _json

            body = _json(self.client.get(f"/platform/tree/persons/{a}/spouse-relationships"))
            for rel in body.get("relationships", []):
                ends = {(rel.get("person1") or {}).get("resourceId"), (rel.get("person2") or {}).get("resourceId")}
                if ends == {a, b} and rel.get("id"):
                    return str(rel["id"])
        except Exception:  # noqa: BLE001
            return None
        return None

    # -- step 9: teardown
    def _try_delete(self, fn, oid: str) -> bool:
        try:
            fn(oid, reason=REASON)
            return True
        except Exception as e:  # noqa: BLE001
            return is_gone_error(e)

    def teardown(self) -> None:
        recs = self.ledger() if self.root and (self.import_dir / "tree-ledger.jsonl").is_file() else []
        ids = all_created_ids(recs)
        for kind, oid in self.extra_rels:
            ids[kind].append(oid)
        w = self.writer
        ok = all([self._try_delete(w.delete_couple, c) for c in ids["couple_id"]]
                 + [self._try_delete(w.delete_child_and_parents, c) for c in ids["child_parents_id"]])
        self.r.step("9a teardown relationships", ok, f"{len(ids['couple_id'])} couple and "
                    f"{len(ids['child_parents_id'])} child-and-parents ids attempted (404/410 counts as gone)")
        to_delete = list(ids["person_id"]) + list(self.ids.values())
        ok = all([self._try_delete(w.delete_person, p) for p in to_delete])
        self.r.step("9b teardown people deleted", ok, f"{len(to_delete)} persons attempted")
        bad = []
        for pid in to_delete:
            try:
                if poll(lambda p=pid: self.status(p), lambda v: v in GONE) not in GONE:
                    bad.append(pid)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{pid} ({describe_error(e)})")
        self.r.step("9c teardown absence verified", not bad, f"not gone: {bad}" if bad else "all gone")
        for root in (self.root, self.root2, self.root3):
            if root:
                try:
                    shutil.rmtree(root)
                    self.r.step("9d teardown scratch archive", not root.exists())
                except OSError as e:
                    self.r.step("9d teardown scratch archive", False, describe_error(e))


def run(report: Report | None = None) -> int:
    report = report or Report()
    from familysearch.errors import FSError

    try:
        drill = Drill(report)  # refuses unless the Brave session is beta
    except FSError as e:
        report.step("0 beta session", False, describe_error(e))
        print(report.summary())
        return 2
    report.step("0 beta session", True, "session answers on beta")
    step = None
    try:
        for step in (drill.step1_setup, drill.step2_plan, drill.step3_dry_run, drill.step3b_not_a_couple, drill.step4_wrong_hash,
                     drill.step5_execute, drill.step6_rerun, drill.step7_name_exists, drill.step8_rollback):
            if not step():
                break
        if drill.ids:
            drill.observe()
    except Exception as e:  # noqa: BLE001 - a crash is a failed step; teardown still runs
        report.step(f"crash in {getattr(step, '__name__', 'step')}", False, describe_error(e))
    finally:
        try:
            drill.teardown()
        except Exception as e:  # noqa: BLE001
            report.step("9 teardown crashed", False, describe_error(e))
        for line in drill.observed:
            print(scrub("OBSERVED " + line))
    print(report.summary())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(run())
