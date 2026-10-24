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
from typing import Any

from familysearch import FACT_BIRTH, FACT_DEATH, build_fact, build_person

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


def _reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reason is required: a non-blank change message (str)")
    if len(value) > _REASON_MAX:
        raise ValueError(f"reason must be at most {_REASON_MAX} characters")
    return value.strip()


def _text(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    if len(value) > _TEXT_MAX:
        raise ValueError(f"{name} must be at most {_TEXT_MAX} characters")
    return value


def _required_text(name: str, value: Any) -> str:
    out = _text(name, value)
    if out is None or not out.strip():
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

    # MATCH GATE: any hit blocks creation until Ben has reviewed the candidates.
    if not create_despite_matches:
        hits = ctx.call(
            lambda s: s.search.tree_search(
                given=given, surname=surname, birth_date=birth_date,
                birth_place=birth_place, count=_MAX_CANDIDATES,
            )
        )
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
        birth_date, death_date, today_year=_current_year(),
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
    rels = ctx.call(
        lambda s: s.writer.create_parent_child(child_id=child, parent_ids=parents, reason=reason)
    )
    return {"status": "created", "relationship_ids": list(rels)}


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

        `gender` is Male, Female or Unknown. Every write needs a `reason`: a short
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
        URI). At least one of `date` or `place` is required. Every write needs a
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
