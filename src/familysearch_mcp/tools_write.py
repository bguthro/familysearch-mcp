"""Guarded write tools: thin wrappers over TreeWriter with safety gates.

These tools change the real, shared FamilySearch family tree, so every tool
begins with the write gate (ctx.require_writes), validates all input at the
boundary, and only then reaches the library through ctx.call. Errors from the
library or Context are deliberately not caught (fail closed); the ONLY caught
exception is the ContextError from the write gate itself.

There is deliberately no delete or merge tool.
"""

from __future__ import annotations

import datetime
import unicodedata
from typing import Any

from familysearch import FACT_BIRTH, FACT_DEATH, FSError, build_fact, build_person

from .context import ContextError
from .guards import classify_life_status
from .projections import project_search_hit
from .validation import validate_person_id

_REASON_MAX = 500
_TEXT_MAX = 2000
_GENDERS = {"Male", "Female", "Unknown"}
_GEDCOMX = "http://gedcomx.org/"
_FACT_NAMES = (
    "Adoption", "Baptism", "BarMitzvah", "BatMitzvah", "Birth", "Blessing", "Burial",
    "Census", "Christening", "Circumcision", "Confirmation", "Cremation", "Death",
    "Education", "Emigration", "Ethnicity", "FirstCommunion", "Funeral", "Immigration",
    "MaritalStatus", "Marriage", "MilitaryService", "Mission", "Naturalization",
    "Nationality", "Occupation", "Ordination", "Residence", "Retirement",
)
_FACTS_BY_LOWER = {n.lower(): n for n in _FACT_NAMES}
_MAX_CANDIDATES = 10


def _current_year() -> int:
    return datetime.date.today().year


def _gate(ctx: Any) -> dict | None:
    """Write gate. Only the ContextError raised by require_writes is caught."""
    try:
        ctx.require_writes()
    except ContextError as e:
        return {"status": "writes_disabled", "message": str(e)}
    return None


class _BadResponse(TypeError):
    """The library returned something other than a list of non-empty ids."""


class PartialWriteError(FSError):
    """A multi-step write failed part-way; some relationships already exist."""

    def __init__(self, message: str, created_ids: list[str]) -> None:
        super().__init__(message)
        self.created_ids = list(created_ids)


def _no_control(name: str, value: str) -> None:
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in value):
        raise ValueError(f"{name} must not contain control characters")


def _reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reason is required: a non-blank change message (str)")
    value = value.strip()
    if len(value) > _REASON_MAX:
        raise ValueError(f"reason must be at most {_REASON_MAX} characters")
    _no_control("reason", value)
    return value


def _text(name: str, value: Any) -> str | None:
    """Stripped free text; blank becomes None. Rejects control characters."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    value = value.strip()
    if len(value) > _TEXT_MAX:
        raise ValueError(f"{name} must be at most {_TEXT_MAX} characters")
    _no_control(name, value)
    return value or None


_CONFIRM_HINT = (
    "omit it (use confirm_deceased=True for a deceased person whose death date is unknown)"
)
_PLACE_ONLY_HINT = "omit the date to record place only"


def _past_date(name: str, value: str | None, year: int, hint: str = _CONFIRM_HINT) -> None:
    if value is not None and classify_life_status(None, value, today_year=year) != "deceased":
        raise ValueError(f"{name} is not a recognizable past date; {hint}")


def _required_text(name: str, value: Any) -> str:
    out = _text(name, value)
    if out is None:
        raise ValueError(f"{name} is required")
    return out


def _gender(value: Any) -> str:
    if isinstance(value, str):
        g = value.strip().title()
        if g in _GENDERS:
            return g
    raise ValueError("gender must be one of 'Male', 'Female', 'Unknown'")


def _fact_type(value: Any) -> tuple[str, str]:
    """Return (canonical short name, full gedcomx URI) or raise ValueError."""
    if isinstance(value, str):
        v = value.strip()
        if v.startswith(_GEDCOMX):
            v = v[len(_GEDCOMX):]
        name = _FACTS_BY_LOWER.get(v.lower())
        if name:
            return name, _GEDCOMX + name
    raise ValueError(
        f"unsupported fact_type {value!r}; allowed: {', '.join(_FACT_NAMES)}"
    )


def _confidence_key(candidate: dict) -> tuple[int, int]:
    c = candidate.get("confidence")
    return (1, c) if isinstance(c, int) else (0, 0)


def _create_person(ctx: Any, given, surname, gender, reason, birth_date, birth_place,
                   death_date, living, create_despite_matches, confirm_deceased) -> dict:
    refused = _gate(ctx)
    if refused:
        return refused
    reason = _reason(reason)
    given = _required_text("given", given)
    surname = _required_text("surname", surname)
    gender = _gender(gender)
    birth_date = _text("birth_date", birth_date)
    birth_place = _text("birth_place", birth_place)
    death_date = _text("death_date", death_date)
    if living is not None and not isinstance(living, bool):
        raise ValueError("living must be true, false or null")
    if not isinstance(create_despite_matches, bool) or not isinstance(confirm_deceased, bool):
        raise ValueError("create_despite_matches and confirm_deceased must be booleans")
    if living is True and confirm_deceased:
        raise ValueError("living=True and confirm_deceased=True contradict each other")
    year = _current_year()
    _past_date("death_date", death_date, year)

    # MATCH GATE: any hit blocks creation until Ben has reviewed the candidates.
    if not create_despite_matches:
        hits = ctx.call(
            lambda s: s.search.tree_search(
                given=given, surname=surname, birth_date=birth_date,
                birth_place=birth_place, count=_MAX_CANDIDATES,
            )
        )
        if not isinstance(hits, list):
            raise FSError("tree_search returned an unexpected response; refusing to create")
        if hits:
            candidates = [project_search_hit(e) for e in hits]
            candidates.sort(key=_confidence_key, reverse=True)
            return {
                "status": "matches_found",
                "candidates": candidates[:_MAX_CANDIDATES],
                "message": (
                    "Possible existing matches were found in the tree. Nothing was "
                    "created. Show these candidates to Ben; only retry with "
                    "create_despite_matches=True after Ben says none of them is the "
                    "same person."
                ),
            }

    # LIFE STATUS: living=False is not a bypass; only living=True is an assertion.
    classification = classify_life_status(
        birth_date, death_date, today_year=year,
        asserted_living=(living is True),
    )
    if classification == "living":
        is_living = True
    elif classification in ("deceased", "deceased_by_age"):
        is_living = False
    elif confirm_deceased:  # possibly_living, with Ben's explicit confirmation
        is_living = False
    else:
        return {
            "status": "needs_life_confirmation",
            "classification": classification,
            "message": (
                "This person may be living: born within about 110 years with no "
                "recorded death, or the living/death inputs conflict. Nothing was "
                "created. Ask Ben. Retry with living=True to create a PRIVATE living "
                "person, or with confirm_deceased=True once Ben confirms the person "
                "is deceased."
            ),
        }

    facts = []
    if birth_date or birth_place:
        facts.append(build_fact(FACT_BIRTH, date=birth_date, place=birth_place))
    if death_date:
        facts.append(build_fact(FACT_DEATH, date=death_date))
    person = build_person(
        given=given, surname=surname, gender=gender, living=is_living,
        facts=facts or None,
    )
    new_id = ctx.call(lambda s: s.writer.create_person(person, reason=reason))
    return {"status": "created", "id": new_id, "living": is_living,
            "life_status": classification}


def _add_fact(ctx: Any, person_id, fact_type, reason, date, place) -> dict:
    refused = _gate(ctx)
    if refused:
        return refused
    reason = _reason(reason)
    pid = validate_person_id(person_id)
    short, uri = _fact_type(fact_type)
    date = _text("date", date)
    place = _text("place", place)
    if date is None and place is None:
        raise ValueError("add_fact needs at least one of date or place")
    if short == "Death":
        _past_date("a Death fact's date", date, _current_year(), _PLACE_ONLY_HINT)
    fact = build_fact(uri, date=date, place=place)
    ctx.call(lambda s: s.writer.add_fact(pid, fact, reason=reason))
    return {"status": "added", "person_id": pid, "fact_type": short}


def _create_couple(ctx: Any, person1_id, person2_id, reason) -> dict:
    refused = _gate(ctx)
    if refused:
        return refused
    reason = _reason(reason)
    p1 = validate_person_id(person1_id)
    p2 = validate_person_id(person2_id)
    if p1 == p2:
        raise ValueError("a couple needs two different persons")
    rel = ctx.call(lambda s: s.writer.create_couple(p1, p2, reason=reason))
    return {"status": "created", "relationship_id": rel}


def _create_parent_child(ctx: Any, child_id, parent_ids, reason) -> dict:
    refused = _gate(ctx)
    if refused:
        return refused
    reason = _reason(reason)
    child = validate_person_id(child_id)
    if not isinstance(parent_ids, list) or not 1 <= len(parent_ids) <= 2:
        raise ValueError("parent_ids must be a list of 1 or 2 person ids")
    parents = [validate_person_id(p) for p in parent_ids]
    if len(set(parents)) != len(parents):
        raise ValueError("parent_ids must not contain duplicates")
    if child in parents:
        raise ValueError("a person cannot be their own parent")
    # One ctx.call per parent: a failure (or a token-expiry re-run) on a later
    # parent must never re-post an earlier, already-created relationship.
    created: list[str] = []
    for parent in parents:
        try:
            rels = ctx.call(
                lambda s, p=parent: s.writer.create_parent_child(
                    child_id=child, parent_ids=[p], reason=reason)
            )
            if not (isinstance(rels, list) and rels
                    and all(isinstance(r, str) and r for r in rels)):
                raise _BadResponse("create_parent_child returned an unexpected response")
        except Exception as e:
            if not created:
                if isinstance(e, _BadResponse):
                    raise FSError("unexpected response from create_parent_child") from e
                raise
            raise PartialWriteError(
                f"Relationship(s) already created: {', '.join(created)}. Linking parent "
                f"{parent} FAILED ({e}). Do NOT repeat the whole call; only the failed "
                f"parent {parent} remains to be linked.",
                created,
            ) from e
        created.extend(rels)
    return {"status": "created", "relationship_ids": created}


def register_write_tools(mcp: Any, ctx: Any) -> None:
    @mcp.tool()
    def create_person(
        given: str,
        surname: str,
        gender: str,
        reason: str,
        birth_date: str | None = None,
        birth_place: str | None = None,
        death_date: str | None = None,
        living: bool | None = None,
        create_despite_matches: bool = False,
        confirm_deceased: bool = False,
    ) -> dict:
        """Create a new person in the shared FamilySearch Family Tree (a WRITE).

        `gender` is Male, Female or Unknown. `death_date`, if given, must be a
        recognizable past date (junk like "Unknown" is rejected; for a deceased person
        with no known death date use confirm_deceased=True). Every write needs a `reason`: a short
        change message (max 500 chars) recorded in FamilySearch's change history;
        mention the research archive folder or source when relevant.

        Safety gates (nothing is created when a gate blocks):
        1. The tree is searched first. If ANY possible match exists the result is
           {"status": "matches_found", "candidates": [...]}. Show those candidates to
           Ben and get his explicit OK that none is the same person BEFORE retrying
           with create_despite_matches=True. Never set it on your own.
        2. If the person may be living (born within about 110 years with no recorded
           death) the result is {"status": "needs_life_confirmation"}. Ask Ben. Pass
           living=True to create a living person, who is created PRIVATE (not visible
           on the shared tree). Pass confirm_deceased=True ONLY after Ben explicitly
           confirms the person is deceased (surface the missing death date to him
           first). living=False is not a bypass. living=True with confirm_deceased=True
           is contradictory and rejected.
        Writes may be disabled, in which case {"status": "writes_disabled"} is
        returned. On success returns {"status": "created", "id", "living",
        "life_status"}.
        """
        return _create_person(ctx, given, surname, gender, reason, birth_date,
                              birth_place, death_date, living,
                              create_despite_matches, confirm_deceased)

    @mcp.tool()
    def add_fact(
        person_id: str,
        fact_type: str,
        reason: str,
        date: str | None = None,
        place: str | None = None,
    ) -> dict:
        """Add a fact (event) to an existing person in the shared Family Tree (a WRITE).

        `fact_type` is a case-insensitive short name from: Adoption, Baptism,
        BarMitzvah, BatMitzvah, Birth, Blessing, Burial, Census, Christening,
        Circumcision, Confirmation, Cremation, Death, Education, Emigration,
        Ethnicity, FirstCommunion, Funeral, Immigration, MaritalStatus, Marriage,
        MilitaryService, Mission, Naturalization, Nationality, Occupation,
        Ordination, Residence, Retirement (or the full http://gedcomx.org/<Name>
        URI). At least one of `date` or `place` is required. A Death fact's `date`,
        if given, must be a recognizable past date (junk like "Unknown" is
        rejected); omit the date to record the place only. Every write needs a
        `reason`: a short change message (max 500 chars) recorded in FamilySearch's
        change history; mention the research archive folder or source when relevant.
        Returns {"status": "added", "person_id", "fact_type"}, or
        {"status": "writes_disabled"} when writes are off.
        """
        return _add_fact(ctx, person_id, fact_type, reason, date, place)

    @mcp.tool()
    def create_couple(person1_id: str, person2_id: str, reason: str) -> dict:
        """Create a couple (spouse) relationship between two different existing
        persons in the shared Family Tree (a WRITE). Every write needs a `reason`: a
        short change message (max 500 chars) recorded in FamilySearch's change
        history; mention the research archive folder or source when relevant.
        Returns {"status": "created", "relationship_id"}, or
        {"status": "writes_disabled"} when writes are off.
        """
        return _create_couple(ctx, person1_id, person2_id, reason)

    @mcp.tool()
    def create_parent_child(child_id: str, parent_ids: list[str], reason: str) -> dict:
        """Link a child to one or two parents in the shared Family Tree (a WRITE).
        `parent_ids` is a list of 1 or 2 distinct person ids that do not include the
        child. Every write needs a `reason`: a short change message (max 500 chars)
        recorded in FamilySearch's change history; mention the research archive
        folder or source when relevant. Returns {"status": "created",
        "relationship_ids": [...]}, or {"status": "writes_disabled"} when writes
        are off.
        """
        return _create_parent_child(ctx, child_id, parent_ids, reason)
