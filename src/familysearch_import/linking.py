"""Offline link verdict logic: compare archive people to FamilySearch persons. Pure."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .scan import IdCandidate, Person, mine_candidates, nfc

FsStatus = Literal["ok", "redirected", "deleted", "not-found", "error"]
Compare = Literal["confirmed", "probable", "mismatch"]
Verdict = Literal["confirmed", "probable", "conflict", "unlinked", "needs-search"]

_KIND_RANK = {"webloc": 0, "note": 1, "filename": 2}
_FILLER = {"unknown", "living", "private"}


@dataclass(frozen=True)
class FsPerson:
    id: str
    name: str
    birth_year: int | None
    death_year: int | None
    living: bool | None
    status: FsStatus
    survivor_id: str | None = None


@dataclass(frozen=True)
class LinkResult:
    person: Person
    # (candidate, FamilySearch person, outcome) where outcome is a compare verdict
    # for usable ids or the FsPerson status ("not-found", "deleted", "error", ...) otherwise.
    candidates: tuple[tuple[IdCandidate, FsPerson, str], ...]
    verdict: Verdict
    chosen_id: str | None
    # Evidence only, filled by the report builder for needs-search people; never accepted.
    search: tuple[FsPerson, ...] = ()
    search_error: bool = False


def _tokens(text: str) -> list[str]:
    s = unicodedata.normalize("NFKD", nfc(text)).casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _FILLER]


def _split_parens(name: str) -> tuple[list[str], list[str]]:
    inside = re.findall(r"\(([^)]*)\)", name)
    outside = re.sub(r"\([^)]*\)", " ", name)
    return _tokens(outside), [t for part in inside for t in _tokens(part)]


def name_matches(archive_name: str, fs_name: str) -> bool:
    main, alt = _split_parens(archive_name)
    fs = _tokens(fs_name.replace("(", " ").replace(")", " "))
    if not main or not fs:
        return False
    fs_set, main_set = set(fs), set(main)
    if main_set <= fs_set:
        return True
    if len(fs) >= 2 and fs_set <= main_set | set(alt):
        return True
    return len(main) >= 2 and len(fs) >= 2 and main[0] == fs[0] and main[-1] == fs[-1]


def _year_delta(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else abs(a - b)


def compare(person: Person, fs: FsPerson) -> Compare:
    if not name_matches(person.display_name, fs.name):
        return "mismatch"
    deltas = [
        d
        for d in (
            _year_delta(person.birth, fs.birth_year),
            _year_delta(person.death, fs.death_year),
        )
        if d is not None
    ]
    if any(d > 2 for d in deltas):
        return "mismatch"
    if not deltas or any(d > 0 for d in deltas):
        return "probable"
    return "confirmed"


def resolve_links(person: Person, lookups: Mapping[str, FsPerson]) -> LinkResult:
    rows: list[tuple[IdCandidate, FsPerson, str]] = []
    usable: list[tuple[str, str, int]] = []  # (verdict, chosen id, kind rank)
    errored = False
    for cand in sorted(mine_candidates(person), key=lambda c: _KIND_RANK[c.kind]):
        fs = lookups.get(cand.id)
        if fs is None:
            continue
        if fs.status == "redirected":
            survivor = fs.survivor_id
            if not survivor:
                rows.append((cand, fs, "redirected"))
                continue
            target = lookups.get(survivor, fs)
            outcome, chosen = compare(person, target), survivor
        elif fs.status == "ok":
            outcome, chosen = compare(person, fs), fs.id
        else:
            errored = errored or fs.status == "error"
            rows.append((cand, fs, fs.status))
            continue
        rows.append((cand, fs, outcome))
        if outcome != "mismatch":
            usable.append((outcome, chosen, _KIND_RANK[cand.kind]))
    candidates = tuple(rows)
    confirmed = list(dict.fromkeys(c for v, c, _ in usable if v == "confirmed"))
    if len(confirmed) > 1:
        return LinkResult(person, candidates, "conflict", None)
    if confirmed:
        return LinkResult(person, candidates, "confirmed", confirmed[0])
    probable = [c for v, c, _ in usable if v == "probable"]
    if probable:
        return LinkResult(person, candidates, "probable", probable[0])
    if errored:
        return LinkResult(person, candidates, "unlinked", None)
    return LinkResult(person, candidates, "needs-search", None)
