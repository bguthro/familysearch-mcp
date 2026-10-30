"""Dry-run import planner: additive actions only. No network of its own, no writes.

The reader (a ReadOnlyFS or a fake) is the only door to FamilySearch.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .classify import classify_document
from .prod_reader import ReadFailed
from .record import record_state_and_status
from .scan import Person, nfc

MAX_BYTES = 15_000_000
UPLOAD_EXTS = frozenset({".pdf", ".jpg", ".jpeg", ".png"})

Kind = Literal["upload-document", "add-source", "skip"]
State = Literal["planned", "likely-covered", "already-present", "skipped"]


@dataclass(frozen=True)
class Action:
    key: str
    kind: Kind
    person_line: str
    person_folder: str
    fs_id: str
    file: str | None
    title: str | None
    url: str | None
    state: State
    reason: str
    sha256: str | None


@dataclass(frozen=True)
class PersonPlan:
    line: str
    folder: str
    fs_id: str | None
    status: str  # planned | nothing-to-do | skipped | unplanned (read error)
    actions: tuple[Action, ...]
    note: str


@dataclass(frozen=True)
class Plan:
    people: tuple[PersonPlan, ...]
    plan_hash: str
    env: str = ""


def current_link(pkey: str, approved: Mapping[str, str], links: Mapping[str, str | None]) -> str | None:
    """The one rule for which FamilySearch id a person folder is linked to (planner and executor)."""
    return approved.get(pkey) or links.get(pkey)


def _norm(s: str | None) -> str:
    return " ".join(nfc(s or "").lower().split())


def action_key(fs_id: str, kind: str, discriminator: str) -> str:
    return hashlib.sha256(f"{fs_id}\0{kind}\0{discriminator}".encode("utf-8")).hexdigest()


def compute_plan_hash(people: Sequence[PersonPlan], env: str = "") -> str:
    planned = sorted(
        (asdict(a) for p in people for a in p.actions if a.state in ("planned", "likely-covered")),
        key=lambda d: d["key"],
    )
    blob = json.dumps({"env": env, "actions": planned}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(person: Person, f: Path) -> str:
    try:
        return nfc(str(f.relative_to(person.path)))
    except ValueError:
        return nfc(f.name)


def _skipped(person: Person, reason: str, fs_id: str | None, status: str = "skipped") -> PersonPlan:
    return PersonPlan(person.line, person.folder, fs_id, status, (), reason)


def build_plan(
    people: Sequence[Person],
    links: Mapping[str, str | None],
    approved: Mapping[str, str],
    reader,
    ledger_keys: frozenset[str] = frozenset(),
    max_bytes: int = MAX_BYTES,
    force_upload: frozenset[str] = frozenset(),
    env: str = "",
) -> Plan:
    out = [_plan_person(p, links, approved, reader, ledger_keys, max_bytes, force_upload) for p in people]
    out = _dedupe_shared_files(out)
    return Plan(tuple(out), compute_plan_hash(out, env), env)


def _person_sort_key(line: str, folder: str) -> tuple[str, str]:
    return nfc(line), nfc(folder)


def _dedupe_shared_files(people: list[PersonPlan]) -> list[PersonPlan]:
    """FamilySearch stores an identical file once (HTTP 409 otherwise), even across people.

    Across all people, when one sha256 would be uploaded under more than one action key, only the
    action of the person sorting first by (line, folder) stays; the rest become skipped. Winner
    choice ignores the ledger, so the plan does not depend on run progress. The same person under
    two lines shares one key and is left alone.
    """
    live = ("planned", "likely-covered", "already-present")
    groups: dict[str, list[Action]] = {}
    for p in people:
        for a in p.actions:
            if a.kind == "upload-document" and a.sha256 and a.state in live:
                groups.setdefault(a.sha256, []).append(a)
    demote: dict[tuple[str, str, str], str] = {}
    for acts in groups.values():
        if len({a.key for a in acts}) < 2:
            continue
        win = min(acts, key=lambda a: (_person_sort_key(a.person_line, a.person_folder), a.key))
        for a in acts:
            if a.key != win.key and a.state in ("planned", "likely-covered"):
                demote[(a.person_line, a.person_folder, a.key)] = (
                    f"same file already planned for {nfc(win.person_line)}/{nfc(win.person_folder)} "
                    "(FamilySearch stores an identical image once; attach manually)")
    if not demote:
        return people
    out = []
    for p in people:
        acts = tuple(
            _with(a, state="skipped", reason=demote[(a.person_line, a.person_folder, a.key)])
            if (a.person_line, a.person_folder, a.key) in demote else a
            for a in p.actions)
        if acts != p.actions:
            status = p.status
            if p.status == "planned" and not any(a.state == "planned" for a in acts):
                status = "nothing-to-do"
            p = PersonPlan(p.line, p.folder, p.fs_id, status, acts, p.note)
        out.append(p)
    return out


def _plan_person(person, links, approved, reader, ledger_keys, max_bytes, force_upload=frozenset()) -> PersonPlan:
    pkey = f"{person.line}/{person.folder}"
    status = record_state_and_status(person)[0]
    if status == "living":
        return _skipped(person, "living", None)
    if status != "deceased":
        return _skipped(person, "unknown status", None)
    fs_id = current_link(pkey, approved, links)
    if not fs_id:
        return _skipped(person, "no confirmed link", None)

    fs = reader.get_fs_person(fs_id)
    if fs.status == "redirected":
        return _skipped(person, f"approved id redirected to {fs.survivor_id}", fs_id)
    if fs.status in ("not-found", "deleted"):
        return _skipped(person, f"id no longer resolves ({fs.status})", fs_id)
    if fs.status != "ok":
        return _skipped(person, "unplanned (read error)", fs_id, "unplanned (read error)")
    if fs.living is True:
        return _skipped(person, "living", fs_id)

    candidates = _candidates(person, fs_id, max_bytes)
    if not any(c[0] in ("upload", "cite") for c in candidates):
        return PersonPlan(person.line, person.folder, fs_id, "nothing-to-do",
                          tuple(c[1] for c in candidates), "")
    try:
        titles = {_norm(t) for t in reader.get_memory_titles(fs_id)}
        refs = reader.get_source_refs(fs_id)
    except ReadFailed:  # token/link errors stay fatal
        return _skipped(person, "unplanned (read error)", fs_id, "unplanned (read error)")
    ref_urls = {r.url for r in refs if r.url}
    ref_titles = {_norm(r.title) for r in refs if r.title}

    actions: list[Action] = []
    seen_sha: set[str] = set()
    seen_url: set[str] = set()
    stem_count: dict[str, int] = {}
    for tag, act in candidates:
        if tag == "upload":
            if act.sha256 in seen_sha:
                actions.append(_with(act, state="skipped", reason="duplicate content"))
                continue
            seen_sha.add(act.sha256)
            stem = _norm(act.title)
            n = stem_count[stem] = stem_count.get(stem, 0) + 1
            reason = act.reason
            if n > 1:
                act = _with(act, title=f"{act.title}-{n}")
                reason = f"title collision with another file, renamed {act.title}"
            if act.key in ledger_keys:
                act = _with(act, state="already-present", reason="in ledger")
            elif _norm(act.title) in titles:
                act = _with(act, state="already-present", reason="memory with this title exists")
            else:
                act = _with(act, reason=reason)
                covering = _covering_source(act.file or "", refs)
                forced = f"{person.line}/{person.folder}/{act.file}" in force_upload
                if covering and not forced:
                    act = _with(act, state="likely-covered",
                                reason=f"FamilySearch already has source: {covering[:80]}")
            actions.append(act)
        elif tag == "cite":
            if act.url in seen_url:
                actions.append(_with(act, state="skipped", reason="duplicate citation"))
                continue
            seen_url.add(act.url)
            if act.key in ledger_keys:
                act = _with(act, state="already-present", reason="in ledger")
            elif act.url in ref_urls or _norm(act.title) in ref_titles:
                act = _with(act, state="already-present", reason="source already cited")
            actions.append(act)
        else:
            actions.append(act)
    planned = any(a.state == "planned" for a in actions)
    return PersonPlan(person.line, person.folder, fs_id,
                      "planned" if planned else "nothing-to-do", tuple(actions), "")


# (file-name words, source-title words); first word of a pair set must appear in each side
_KINDS = (
    (("census",), ("census",)),
    (("draft",), ("draft",)),
    (("death",), ("death",)),
    (("birth",), ("birth",)),
    (("marriage",), ("marriage",)),
    (("naturalization", "naturalisation"), ("naturalization", "naturalisation")),
    (("passenger", "manifest"), ("passenger", "manifest")),
    (("household examination",), ("household examination",)),
    (("baptism", "christening"), ("baptism", "christening")),
    (("obituary", "obit"), ("obituary",)),
)
_YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")


def _covering_source(filename: str, refs) -> str | None:
    """Title of an existing FS source that likely covers this document, else None."""
    name = _norm(Path(filename).stem)
    years = set(_YEAR.findall(name))
    for file_words, title_words in _KINDS:
        if not any(re.search(rf"\b{re.escape(w)}", name) for w in file_words):
            continue
        for r in refs:
            t = _norm(r.title)
            if any(w in t for w in title_words) and (not years or years & set(_YEAR.findall(t))):
                return r.title
    return None


def _with(a: Action, **kw) -> Action:
    d = asdict(a)
    d.update(kw)
    return Action(**d)


def _candidates(person: Person, fs_id: str, max_bytes: int) -> list[tuple[str, Action]]:
    out: list[tuple[str, Action]] = []
    for f in sorted(person.files, key=_rel_key(person)):
        rel = _rel(person, f)
        v = classify_document(person, f, "deceased")
        if v.reason in ("not a document file", "citation sheet"):
            continue

        def skip(reason: str, sha: str | None = None) -> tuple[str, Action]:
            return "skip", Action(action_key(fs_id, "skip", rel), "skip", person.line, person.folder,
                                  fs_id, rel, None, None, "skipped", reason, sha)

        if v.action == "skip":
            out.append(skip(v.reason))
        elif v.action == "cite-only":
            url = v.source_url or ""
            out.append(("cite", Action(action_key(fs_id, "add-source", url), "add-source", person.line,
                                       person.folder, fs_id, rel, nfc(f.stem), url, "planned",
                                       v.reason, None)))
        else:
            try:
                size = f.stat().st_size
            except OSError:
                out.append(skip("unreadable"))
                continue
            if f.suffix.lower() not in UPLOAD_EXTS or size > max_bytes:
                out.append(skip("unsupported"))
                continue
            try:
                sha = _file_sha(f)
            except OSError:
                out.append(skip("unreadable"))
                continue
            out.append(("upload", Action(action_key(fs_id, "upload-document", sha), "upload-document",
                                         person.line, person.folder, fs_id, rel, nfc(f.stem), None,
                                         "planned", v.reason, sha)))
    return out


def _rel_key(person: Person):
    return lambda f: _rel(person, f)
