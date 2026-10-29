"""Offline link verdict logic: compare archive people to FamilySearch persons. Pure."""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .scan import IdCandidate, Person, mine_candidates, nfc

FsStatus = Literal["ok", "redirected", "deleted", "not-found", "error"]
Compare = Literal["confirmed", "probable", "mismatch"]
Verdict = Literal["confirmed", "probable", "conflict", "unlinked", "linked-differs", "needs-search"]

_KIND_RANK = {"webloc": 0, "note": 1, "filename": 2}
_TITLES = {"rev", "reverend", "elder", "dr", "sr", "jr", "mr", "mrs", "capt", "col", "gen", "hon"}
_FILLER = {"unknown", "living", "private"} | _TITLES
_NICKNAMES = (
    {"bess", "bessie", "elizabeth", "betsy", "betty"},
    {"will", "william", "bill"},
    {"jack", "john"},
    {"jim", "james"},
    {"peggy", "margaret"},
    {"kate", "katherine", "catherine"},
    {"winnie", "winifred"},
)
_QUOTED_RE = re.compile(r'["“]([^"”]*)["”]')
FUZZY = 0.8


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


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", nfc(text)).casefold()
    return "".join(c for c in s if not unicodedata.combining(c))


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _fold(text)) if t and t not in _FILLER]


def _split_parens(name: str) -> tuple[list[str], list[str]]:
    inside = re.findall(r"\(([^)]*)\)", name)
    outside = re.sub(r"\([^)]*\)", " ", name)
    return _tokens(outside), [t for part in inside for t in _tokens(part)]


_ROMAN = {"i", "ii", "iii"}


@dataclass(frozen=True)
class _Name:
    given: str | None
    surname: str | None
    alts: tuple[str, ...]  # folded tokens from parenthesised alternate names
    shown_given: str
    shown_surname: str


def _parse_name(name: str) -> _Name:
    outside = _QUOTED_RE.sub(" ", re.sub(r"\([^)]*\)", " ", name))
    parts = re.findall(r"\(([^)]*)\)", name) + _QUOTED_RE.findall(name)
    alts = tuple(t for part in parts for t in _tokens(part))
    pairs = [(o, _fold(o)) for o in re.split(r"[^\w]+", nfc(outside)) if o]
    # drop initials, roman numerals and filler words
    pairs = [(o, f) for o, f in pairs if len(f) > 1 and f not in _ROMAN and f not in _FILLER]
    if not pairs:
        return _Name(None, None, alts, "", "")
    g, gf = pairs[0]
    if len(pairs) == 1:
        return _Name(gf, None, alts, g, "")
    s, sf = pairs[-1]
    return _Name(gf, sf, alts, g, s)


def _canon(s: str) -> str:
    """Collapse common spelling-variant patterns (Aeschlimann ~ Eschliman)."""
    s = s.replace("sch", "sh").replace("ae", "e").replace("ou", "o")
    s = re.sub(r"(.)\1", r"\1", s)  # doubled letters
    return re.sub(r"[xst]$", "", s)


def _sim(a: str, b: str) -> float:
    raw = difflib.SequenceMatcher(None, a, b).ratio()
    return max(raw, difflib.SequenceMatcher(None, _canon(a), _canon(b)).ratio())


def _close(a: str | None, b: str | None, extra: Sequence[str] = ()) -> bool:
    if not a or not b:
        return False
    return any(_sim(b, x) >= FUZZY for x in (a, *extra))


def _name_quality(person_name: str, fs_name: str) -> int:
    """0 incompatible, 1 compatible with spelling variation, 2 same names."""
    a, b = _parse_name(person_name), _parse_name(fs_name)
    if not a.given or not b.given:
        return 0
    given_ok = _close(a.given, b.given, a.alts) or any(
        x in g and b.given in g for g in _NICKNAMES for x in (a.given, *a.alts)
    )
    if a.surname is None or b.surname is None:
        if not given_ok:
            return 0
        return 2 if a.given == b.given or b.given in a.alts else 1
    surname_ok = _close(a.surname, b.surname, a.alts)
    if not (given_ok and surname_ok):
        return 0
    exact = (a.given == b.given or b.given in a.alts) and (a.surname == b.surname or b.surname in a.alts)
    return 2 if exact else 1


def name_matches(archive_name: str, fs_name: str) -> bool:
    return _name_quality(archive_name, fs_name) > 0


def differences(person: Person, fs: FsPerson) -> tuple[str, ...]:
    """Short human-readable differences between the archive person and a FamilySearch person."""
    out: list[str] = []
    a, b = _parse_name(person.display_name), _parse_name(fs.name)
    if a.given and b.given and a.given != b.given and b.given not in a.alts:
        out.append(f"given name: {a.shown_given} vs {b.shown_given}")
    if a.surname and b.surname and a.surname != b.surname and b.surname not in a.alts:
        out.append(f"surname spelling: {a.shown_surname} vs {b.shown_surname}")
    if person.birth is not None and fs.birth_year is not None and person.birth != fs.birth_year:
        out.append(f"birth {person.birth} vs {fs.birth_year}")
    if person.death is not None and fs.death_year is not None and person.death != fs.death_year:
        out.append(f"death {person.death} vs {fs.death_year}")
    return tuple(out)


def _year_delta(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else abs(a - b)


def compare(person: Person, fs: FsPerson) -> Compare:
    quality = _name_quality(person.display_name, fs.name)
    if quality == 0:
        return "mismatch"
    birth = _year_delta(person.birth, fs.birth_year)
    if birth is not None and birth > 2:
        return "mismatch"
    death = _year_delta(person.death, fs.death_year)
    if birth is None and death is None:
        return "probable"  # nothing to corroborate the name
    if quality == 2 and not birth and not death:
        return "confirmed"
    return "probable"


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
    if any(c.kind == "webloc" and o == "mismatch" for c, _, o in candidates):
        return LinkResult(person, candidates, "linked-differs", None)
    return LinkResult(person, candidates, "needs-search", None)
