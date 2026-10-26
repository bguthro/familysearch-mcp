"""Helpers shared by the write recipes: read-backs, gone checks, markers and bodies.

A write recipe proves its operation by reading the state back from the server
(``read_person_view``, ``person_sources``, ``person_memories``) before and after the
write, never by the write's status. Each read-back keeps the ``ApiResult`` it was
parsed from, so a failure can carry the response skeleton (``describe``) the
controller needs to fix a recipe from a live run. Markers (``token``) are fresh
``ZZSPIKE`` values: a read-back can only show one if the write put it there.

The request bodies are the shapes the hand-written library proved on beta
(``familysearch.tree.TreeWriter.add_fact`` and ``Sources.attach_to_person`` POST
``{"persons": [{...}]}`` to the person resource; ``build_name`` always emits name
parts), plus the spec's rule for an update: a conclusion that carries its id is
updated, one without an id is created.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from familysearch import FSHTTPError
from familysearch.api._runtime import ApiResult
from familysearch.models import GEDCOMX, GENDER_URI, build_name

from ..core import VerifyContext
from ..reads import skeleton

GONE = (404, 410)
FACT_RESIDENCE = GEDCOMX + "Residence"
# The person -> source link lagged on beta (P2 Task 4 run 1); polled reads wait this
# long at most: (attempts, seconds between them), as REFERENCE_EVENTUAL in the reads.
EVENTUAL = (8, 5.0)
sleep = time.sleep  # the pause between polls (tests replace it)

T = TypeVar("T")


def token(tag: str) -> str:
    """A fresh marker: ``ZZSPIKE`` + an upper-case tag letter + 12 decimal digits.

    Digits only after the prefix (nothing a server could re-case), and no marker is
    contained in another of the same length.
    """
    if not (isinstance(tag, str) and len(tag) == 1 and tag.isalpha()):
        raise ValueError("tag must be one letter")
    return f"ZZSPIKE{tag.upper()}{uuid.uuid4().int % 10**12:012d}"


def status_of(read: Callable[[], ApiResult]) -> int:
    """The status of a read: its own when it answers, the HTTP error status when not."""
    try:
        return read().status
    except FSHTTPError as e:
        return e.status


def gone(read: Callable[[], ApiResult]) -> bool:
    """True when the read answers 404/410, False when it answers; other errors propagate."""
    try:
        read()
    except FSHTTPError as e:
        if e.status in GONE:
            return True
        raise
    return False


def describe(result: ApiResult | None) -> str:
    """The response skeleton for a failure message (``(no response)`` without one)."""
    return "response: " + (skeleton(result) if result is not None else "(no response)")


# ------------------------------------------------------------------ person read-back


@dataclass(frozen=True)
class Fact:
    """A fact conclusion as the server answers it; ``text`` is its JSON, for markers."""

    type: str | None
    date: str | None
    place: str | None
    value: str | None
    text: str = field(compare=False, repr=False)

    def has(self, marker: str) -> bool:
        return marker in self.text


@dataclass(frozen=True)
class PersonView:
    """What readPerson answers for one person: conclusions keyed by their ids.

    ``names`` maps a name id to its first name form's ``fullText``; ``facts`` a fact
    id to its ``Fact``; ``gender`` is ``(id, type)``. ``result`` is the read itself
    (kept out of comparisons).
    """

    names: Mapping[str, str]
    facts: Mapping[str, Fact]
    gender: tuple[str | None, str | None]
    result: ApiResult = field(compare=False, repr=False)

    @property
    def gender_type(self) -> str | None:
        return self.gender[1]

    def name_has(self, marker: str) -> bool:
        return any(marker in text for text in self.names.values())

    def facts_with(self, marker: str) -> list[str]:
        """The ids of the facts carrying ``marker``."""
        return [fid for fid, fact in self.facts.items() if fact.has(marker)]

    def summary(self) -> str:
        facts = {fid: [f.type, f.date, f.place, f.value] for fid, f in self.facts.items()}
        return json.dumps({"names": dict(self.names), "facts": facts,
                           "gender": list(self.gender)}, ensure_ascii=False)


def _original(value: Any) -> str | None:
    return value.get("original") if isinstance(value, dict) else None


def _conclusions(person: dict, key: str, kind: str, result: ApiResult) -> list[dict]:
    items = person.get(key) or []
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise AssertionError(f"readPerson {key} is not a list of objects; {describe(result)}")
    for item in items:
        if not item.get("id"):
            raise AssertionError(f"readPerson answered a {kind} without an id; "
                                 f"{describe(result)}")
    return items


def person_view(result: ApiResult) -> PersonView:
    """Parse a readPerson answer (``persons[0]``) into a ``PersonView``."""
    persons = (result.body or {}).get("persons") if isinstance(result.body, dict) else None
    if not (isinstance(persons, list) and persons and isinstance(persons[0], dict)):
        raise AssertionError(f"readPerson answered no person; {describe(result)}")
    person = persons[0]
    names = {}
    for name in _conclusions(person, "names", "name", result):
        forms = name.get("nameForms") or [{}]
        names[str(name["id"])] = (forms[0] or {}).get("fullText") or ""
    facts = {}
    for fact in _conclusions(person, "facts", "fact", result):
        facts[str(fact["id"])] = Fact(
            type=fact.get("type"), date=_original(fact.get("date")),
            place=_original(fact.get("place")), value=fact.get("value"),
            text=json.dumps(fact, ensure_ascii=False, sort_keys=True))
    gender = person.get("gender") if isinstance(person.get("gender"), dict) else {}
    return PersonView(names=names, facts=facts,
                      gender=(gender.get("id"), gender.get("type")), result=result)


def read_person_view(ctx: VerifyContext, pid: str) -> PersonView:
    return person_view(ctx.api.read_person(pid))


def expect_same(before: Any, after: Any, what: str, view: PersonView | None = None) -> None:
    """A sibling the write must not touch: equal before and after, or fail loudly."""
    if before != after:
        shown = f"; now {view.summary()}" if view is not None else ""
        raise AssertionError(f"{what} changed, but the write should not touch it: "
                             f"{before!r} -> {after!r}{shown}")


# ------------------------------------------------------------------ list read-backs


@dataclass(frozen=True)
class Listing:
    """A list read-back: ``items`` keyed by id, and the read it came from."""

    items: Mapping[str, Any]
    result: ApiResult = field(compare=False, repr=False)


def _sdid(description: Any) -> str:
    """The source description id at the end of a reference's ``description`` URL."""
    return str(description or "").split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]


def person_sources(ctx: VerifyContext, pid: str) -> Listing:
    """readPersonSources: the person's source references, srid -> description id.

    204 (no body) means none. The reference's description is a URL ending in the
    description id (or a ``descriptionId``).
    """
    result = ctx.api.read_person_sources(pid)
    if result.status == 204 or not (result.text or "").strip():
        return Listing({}, result)
    body = result.body if isinstance(result.body, dict) else {}
    refs = {}
    for person in body.get("persons") or []:
        if not isinstance(person, dict) or person.get("id") not in (None, pid):
            continue
        for ref in person.get("sources") or []:
            if isinstance(ref, dict) and ref.get("id"):
                refs[str(ref["id"])] = str(ref.get("descriptionId") or _sdid(
                    ref.get("description")))
    return Listing(refs, result)


def person_memories(ctx: VerifyContext, pid: str) -> Listing:
    """getPersonMemories: the person's memories, memory id -> its title values.

    204 (no body) means none; a memory is a ``sourceDescriptions`` entry.
    """
    result = ctx.api.get_person_memories(pid)
    if result.status == 204 or not (result.text or "").strip():
        return Listing({}, result)
    body = result.body if isinstance(result.body, dict) else {}
    memories = {}
    for desc in body.get("sourceDescriptions") or []:
        if isinstance(desc, dict) and desc.get("id"):
            titles = [t.get("value") for t in desc.get("titles") or [] if isinstance(t, dict)]
            memories[str(desc["id"])] = tuple(v for v in titles if isinstance(v, str))
    return Listing(memories, result)


def poll(ctx: VerifyContext, fetch: Callable[[], T], done: Callable[[T], bool], what: str,
         *, eventual: tuple[int, float] = EVENTUAL,
         result_of: Callable[[T], ApiResult | None] = lambda v: getattr(v, "result", None),
         ) -> T:
    """Read until ``done`` (bounded), for state beta shows only after a lag.

    Errors are never retried. The attempts used go into ``ctx.notes``; running out
    fails with the last response's skeleton.
    """
    attempts, pause = eventual
    value = None
    for n in range(1, attempts + 1):
        value = fetch()
        if done(value):
            ctx.notes.append(f"{what}: after {n} of {attempts} attempts")
            return value
        if n < attempts:
            sleep(pause)
    raise AssertionError(f"{what}: not seen after {attempts} attempts {pause:g}s apart; "
                         f"{describe(result_of(value))}")


# ------------------------------------------------------------------ request bodies


def _person_update(**conclusions: Any) -> dict:
    """The envelope the library posts to the person resource itself."""
    return {"persons": [conclusions]}


def name_update(given: str, surname: str, *, name_id: str | None = None) -> dict:
    """A BirthName (with parts, as beta requires); with ``name_id`` it updates that name."""
    name = build_name(given, surname)
    if name_id:
        name["id"] = name_id
    return _person_update(names=[name])


def fact_update(fact: dict) -> dict:
    """Add ``fact`` (no id: a create), as ``TreeWriter.add_fact`` does."""
    if "id" in fact:
        raise ValueError("a fact to add carries no id")
    return _person_update(facts=[fact])


def gender_update(gender: str, *, gender_id: str | None = None) -> dict:
    if gender not in GENDER_URI:
        raise ValueError(f"gender must be one of {sorted(GENDER_URI)}, got {gender!r}")
    conclusion: dict = {"type": GENDER_URI[gender]}
    if gender_id:
        conclusion["id"] = gender_id
    return _person_update(gender=conclusion)


def source_reference_update(description_url: str) -> dict:
    """Attach a source description, as ``Sources.attach_to_person`` does."""
    return _person_update(sources=[{"description": description_url}])


def note_body(envelope: str, subject: str, text: str) -> dict:
    """A note embedded in its owner, as the fixture world posts it (persons proven)."""
    return {envelope: [{"notes": [{"subject": subject, "text": text}]}]}


def other_gender(gender_type: str | None) -> str:
    """The binary gender a person of ``gender_type`` is changed to."""
    return "Male" if gender_type == GENDER_URI["Female"] else "Female"
