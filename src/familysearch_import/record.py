"""ImportRecord.json schema, validation and draft skeleton generator."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .classify import living_status
from .scan import Person

RECORD_FILE = "ImportRecord.json"
DRAFT_FILE = "ImportRecord.draft.json"

STATUSES = ("deceased", "living")
GENDERS = ("Male", "Female")
FACT_TYPES = (
    "Birth", "Death", "Marriage", "Burial", "Baptism",
    "Residence", "Immigration", "Naturalization", "Occupation",
)
REL_KINDS = ("parent", "spouse", "child")
_ID_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{3,4}$")


class RecordError(ValueError):
    """Invalid ImportRecord; the message starts with the offending field path."""


def _fail(field: str, msg: str):
    raise RecordError(f"{field}: {msg}")


def _check_str(v: Any, field: str, optional: bool = False):
    if v is None and optional:
        return
    if not isinstance(v, str):
        _fail(field, "must be a string")


def _check_obj_list(data: dict, key: str) -> list:
    v = data.get(key)
    if not isinstance(v, list):
        _fail(key, "must be a list")
    for i, item in enumerate(v):
        if not isinstance(item, dict):
            _fail(f"{key}[{i}]", "must be an object")
    return v


def validate_record(data: Any) -> dict:
    if not isinstance(data, dict):
        _fail("record", "must be an object")
    if data.get("version") != 1 or isinstance(data.get("version"), bool):
        _fail("version", "missing or unsupported (must be 1)")
    if data.get("status") not in (*STATUSES, None):
        _fail("status", f"must be one of {STATUSES} or null")
    fid = data.get("familysearch_id")
    if fid is not None and not (isinstance(fid, str) and _ID_RE.match(fid)):
        _fail("familysearch_id", "must be null or an id like ABCD-123")
    if data.get("gender") not in (*GENDERS, None):
        _fail("gender", f"must be one of {GENDERS} or null")
    if not isinstance(data.get("reviewed"), bool):
        _fail("reviewed", "must be a boolean")

    for i, n in enumerate(_check_obj_list(data, "names")):
        _check_str(n.get("given"), f"names[{i}].given")
        _check_str(n.get("surname"), f"names[{i}].surname")
        if "note" in n:
            _check_str(n["note"], f"names[{i}].note")

    for i, f in enumerate(_check_obj_list(data, "facts")):
        if f.get("type") not in FACT_TYPES:
            _fail(f"facts[{i}].type", f"must be one of {FACT_TYPES}")
        for k in ("date", "place"):
            if k in f:
                _check_str(f[k], f"facts[{i}].{k}")
        srcs = f.get("sources")
        if not isinstance(srcs, list) or not all(isinstance(s, str) for s in srcs):
            _fail(f"facts[{i}].sources", "must be a list of strings")

    for i, r in enumerate(_check_obj_list(data, "relationships")):
        if r.get("kind") not in REL_KINDS:
            _fail(f"relationships[{i}].kind", f"must be one of {REL_KINDS}")
        _check_str(r.get("folder"), f"relationships[{i}].folder")
        _check_str(r.get("line"), f"relationships[{i}].line")

    for i, s in enumerate(_check_obj_list(data, "sources")):
        _check_str(s.get("title"), f"sources[{i}].title")
        _check_str(s.get("citation"), f"sources[{i}].citation")
        for k in ("url", "file"):
            if k in s:
                _check_str(s[k], f"sources[{i}].{k}")
    return data


def load_record(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RecordError(f"record: invalid JSON ({e.msg})") from e
    return validate_record(data)


def _split_name(display: str) -> tuple[str, str, str | None]:
    alt = None
    m = re.search(r"\(([^)]*)\)", display)
    if m:
        alt = m.group(1).strip() or None
    base = re.sub(r"\([^)]*\)", " ", display).split()
    if not base:
        return "", "", alt
    if len(base) == 1:
        return "", base[0], alt
    return " ".join(base[:-1]), base[-1], alt


def draft_record(person: Person) -> dict:
    given, surname, alt = _split_name(person.display_name)
    name: dict[str, str] = {"given": given, "surname": surname}
    if alt:
        name["note"] = f"folder alternate: {alt}"
    facts = []
    if person.birth is not None:
        facts.append({"type": "Birth", "date": str(person.birth), "sources": []})
    if person.death is not None:
        facts.append({"type": "Death", "date": str(person.death), "sources": []})
    return {
        "version": 1,
        "status": None,
        "familysearch_id": person.familysearch_ids[0] if len(person.familysearch_ids) == 1 else None,
        "names": [name],
        "gender": None,
        "facts": facts,
        "relationships": [],
        "sources": [],
        "reviewed": False,
    }


def repo_root() -> Path:
    """Directory containing this repo's pyproject.toml."""
    for d in Path(__file__).resolve().parents:
        if (d / "pyproject.toml").is_file():
            return d
    return Path(__file__).resolve().parents[2]


def is_inside(path: Path, root: Path) -> bool:
    p = Path(path).resolve()
    r = Path(root).resolve()
    return p == r or r in p.parents


def write_draft(person: Person, repo_root: Path) -> Path:
    if is_inside(person.path, repo_root):
        raise ValueError(f"refusing to write inside the repo: {person.path}")
    final = person.path / RECORD_FILE
    if final.exists():
        try:
            reviewed = load_record(final).get("reviewed") is True
        except (RecordError, OSError):
            reviewed = True  # unreadable existing record: do not risk it
        if reviewed:
            raise FileExistsError(f"reviewed {RECORD_FILE} exists: {final}")
    draft = person.path / DRAFT_FILE
    with open(draft, "x", encoding="utf-8") as fh:  # 'x' refuses to overwrite
        json.dump(draft_record(person), fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return draft


def record_state_and_status(person: Person) -> tuple[str, str]:
    """(status, record_state); the single place that reads a person's record.

    record_state is missing|draft|invalid|reviewed. A record's "deceased" is
    honoured only when reviewed is true; "living" is always honoured.
    """
    rec = person.path / RECORD_FILE
    if not rec.is_file():
        return living_status(person), "missing"
    try:
        data = load_record(rec)
    except (RecordError, OSError):
        return living_status(person), "invalid"
    reviewed = data["reviewed"]
    status = data.get("status")
    if status == "deceased" and not reviewed:
        status = None
    return living_status(person, status), "reviewed" if reviewed else "draft"


def record_status_for(person: Person) -> str:
    return record_state_and_status(person)[0]
