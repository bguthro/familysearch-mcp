"""Helpers shared by the write recipes: read-backs, gone checks, markers and bodies.

A write recipe proves its operation by reading the state back from the server
(``read_person_view``, ``person_sources``, ``person_memories``; for notes
``note_listing`` / ``single_note``; for relationships ``read_couple``,
``read_child_and_parents`` and a person's ``relatives``; for a source description,
the records citing it, ``citations``) before and after the
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
UNSUPPORTED_MEDIA = 415
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


def _reference_sdid(ref: Mapping[str, Any]) -> str:
    """The description id a source reference names (``descriptionId``, else its URL)."""
    return str(ref.get("descriptionId") or _sdid(ref.get("description")))


# The envelopes readSourceReferences answers citing records in, and their kinds.
CITING = {"persons": "person", "relationships": "couple",
          "childAndParentsRelationships": "child_and_parents"}


@dataclass(frozen=True)
class Citations:
    """readSourceReferences?description={sdid}: the records citing one description.

    ``refs`` maps ``(kind, id)`` to the ids of that record's references to the
    description (empty when the entry names none). 204 (no body) means nobody.
    """

    refs: Mapping[tuple[str, str], frozenset[str]]
    result: ApiResult = field(compare=False, repr=False)


def citations(ctx: VerifyContext, sdid: str) -> Citations:
    result = ctx.api.read_source_references(query={"description": sdid})
    if result.status == 204 or not (result.text or "").strip():
        return Citations({}, result)
    body = result.body if isinstance(result.body, dict) else {}
    refs = {}
    for envelope, kind in CITING.items():
        for entry in body.get(envelope) or []:
            if not (isinstance(entry, dict) and entry.get("id")):
                continue
            refs[(kind, str(entry["id"]))] = frozenset(
                str(s["id"]) for s in entry.get("sources") or []
                if isinstance(s, dict) and s.get("id") and _reference_sdid(s) in (sdid, ""))
    return Citations(refs, result)


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


def note_update(envelope: str, nid: str, subject: str, text: str) -> dict:
    """The same embedded note, carrying the id of the note it updates."""
    return {envelope: [{"notes": [{"id": nid, "subject": subject, "text": text}]}]}


def restore(call: Callable[[Any], ApiResult]) -> ApiResult:
    """A restore: the spec takes */* and answers 204, so a bare POST assumes the least.

    Only if beta refuses it 415 is it retried once with ``{}`` (as the pilot's
    restorePerson). ``call(body)`` sends the restore.
    """
    try:
        return call(None)
    except FSHTTPError as e:
        if e.status != UNSUPPORTED_MEDIA:
            raise
        return call({})


# ------------------------------------------------------------------ notes read-back

ENVELOPES = {"person": "persons", "couple": "relationships",
             "child_and_parents": "childAndParentsRelationships"}


def _owner_notes(result: ApiResult, envelope: str) -> list[dict]:
    body = result.body if isinstance(result.body, dict) else {}
    owners = body.get(envelope)
    if not isinstance(owners, list):
        raise AssertionError(f"the notes are not in {envelope}; {describe(result)}")
    notes = []
    for owner in owners:
        found = owner.get("notes") if isinstance(owner, dict) else None
        notes += [n for n in found or [] if isinstance(n, dict)]
    return notes


def note_listing(result: ApiResult, envelope: str) -> Listing:
    """A notes list read: note id -> its subject. 204 (no body) means none."""
    if result.status == 204 or not (result.text or "").strip():
        return Listing({}, result)
    items = {}
    for note in _owner_notes(result, envelope):
        if not note.get("id"):
            raise AssertionError(f"a listed note has no id; {describe(result)}")
        items[str(note["id"])] = note.get("subject")
    return Listing(items, result)


@dataclass(frozen=True)
class Note:
    """One note as a single-note read answers it."""

    id: str
    subject: str | None
    text: str | None
    result: ApiResult = field(compare=False, repr=False)

    def has(self, marker: str) -> bool:
        return any(marker in (v or "") for v in (self.subject, self.text))


def single_note(result: ApiResult, envelope: str, nid: str) -> Note:
    """A single-note read: exactly the note ``nid``."""
    notes = _owner_notes(result, envelope)
    if [n.get("id") for n in notes] != [nid]:
        raise AssertionError(f"the read of note {nid} answered notes "
                             f"{[n.get('id') for n in notes]}; {describe(result)}")
    note = notes[0]
    return Note(nid, note.get("subject"), note.get("text"), result)


def new_note_id(result: ApiResult) -> str:
    """A created note's id: the Location's last segment, else X-ENTITY-ID (as the
    fixture world, whose note ids beta answered back)."""
    lowered = {k.lower(): v for k, v in result.headers.items()}
    location = (lowered.get("location") or "").split("?")[0].rstrip("/")
    nid = location.rsplit("/", 1)[-1] if location else lowered.get("x-entity-id")
    if not nid:
        raise AssertionError(f"the note create answered no Location or X-ENTITY-ID; "
                             f"{describe(result)}")
    return nid


# ------------------------------------------------------------------ relationship read-back

COUPLE = GEDCOMX + "Couple"
ROLES = {"couple": ("person1", "person2"),
         "child_and_parents": ("parent1", "parent2", "child")}
FACT_KEYS = {"couple": ("facts",), "child_and_parents": ("parent1Facts", "parent2Facts")}


def ref_id(ref: Any) -> str:
    """The person a reference names: its ``resourceId``, else its URI's last segment."""
    if isinstance(ref, dict):
        if isinstance(ref.get("resourceId"), str):
            return ref["resourceId"]
        ref = ref.get("resource")
    if not isinstance(ref, str):
        return ""
    return ref.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _fact(fact: dict) -> Fact:
    return Fact(type=fact.get("type"), date=_original(fact.get("date")),
                place=_original(fact.get("place")), value=fact.get("value"),
                text=json.dumps(fact, ensure_ascii=False, sort_keys=True))


@dataclass(frozen=True)
class Relationship:
    """A relationship as the server answers it.

    ``kind`` is ``couple`` (a ``relationships`` entry typed Couple),
    ``child_and_parents`` (a ``childAndParentsRelationships`` entry) or, for another
    ``relationships`` type, that type. ``roles`` maps each role present (person1 /
    person2, parent1 / parent2 / child) to its person id; ``facts`` maps each fact list
    (``facts``, ``parent1Facts``, ``parent2Facts``) to fact id -> ``Fact``.
    """

    id: str
    kind: str
    roles: Mapping[str, str]
    facts: Mapping[str, Mapping[str, Fact]]
    result: ApiResult = field(compare=False, repr=False)
    # Its source references, srid -> description id, where the read answers them.
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def persons(self) -> frozenset[str]:
        return frozenset(self.roles.values())

    def facts_with(self, marker: str, key: str = "facts") -> list[str]:
        return [fid for fid, f in self.facts.get(key, {}).items() if f.has(marker)]

    def summary(self) -> str:
        facts = {k: {fid: [f.type, f.date, f.place] for fid, f in v.items()}
                 for k, v in self.facts.items()}
        return json.dumps({"id": self.id, "kind": self.kind, "roles": dict(self.roles),
                           "facts": facts}, ensure_ascii=False)


def _relationship(record: dict, envelope: str, result: ApiResult, *,
                  untyped_couple: bool = False) -> Relationship:
    if envelope == "childAndParentsRelationships":
        kind = "child_and_parents"
    elif record.get("type") == COUPLE or (untyped_couple and record.get("type") is None):
        kind = "couple"
    else:
        kind = str(record.get("type"))
    roles = {role: ref_id(record[role]) for role in ROLES.get(kind, ("person1", "person2"))
             if record.get(role)}
    facts = {}
    for key in FACT_KEYS.get(kind, ("facts",)):
        items = record.get(key) or []
        if not isinstance(items, list) or not all(
                isinstance(f, dict) and f.get("id") for f in items):
            raise AssertionError(f"{key} of relationship {record.get('id')} are not facts "
                                 f"with ids; {describe(result)}")
        facts[key] = {str(f["id"]): _fact(f) for f in items}
    sources = {str(s["id"]): _reference_sdid(s) for s in record.get("sources") or []
               if isinstance(s, dict) and s.get("id")}
    return Relationship(str(record.get("id")), kind, roles, facts, result, sources)


def _relationships(result: ApiResult, *, untyped_couple: bool = False
                   ) -> dict[str, Relationship]:
    body = result.body if isinstance(result.body, dict) else {}
    found = {}
    for envelope in ("relationships", "childAndParentsRelationships"):
        for record in body.get(envelope) or []:
            if isinstance(record, dict) and record.get("id"):
                found[str(record["id"])] = _relationship(record, envelope, result,
                                                         untyped_couple=untyped_couple)
    return found


def relationship_view(result: ApiResult, rid: str) -> Relationship:
    """A relationship resource read (readCoupleRelationship and the like): ``rid``."""
    found = _relationships(result)
    if rid not in found:
        raise AssertionError(f"the read of relationship {rid} does not hold it; "
                             f"{describe(result)}")
    return found[rid]


def read_couple(ctx: VerifyContext, crid: str) -> Relationship:
    return relationship_view(ctx.api.read_couple_relationship(crid), crid)


def read_child_and_parents(ctx: VerifyContext, caprid: str) -> Relationship:
    return relationship_view(ctx.api.get_child_and_parents_relationship(caprid), caprid)


@dataclass(frozen=True)
class Relatives:
    """A person's spouses / parents / children read: the persons and relationships it
    lists (204, no body: none)."""

    persons: frozenset[str]
    relationships: Mapping[str, Relationship]
    result: ApiResult = field(compare=False, repr=False)

    def of_kind(self, kind: str) -> dict[str, Relationship]:
        return {rid: r for rid, r in self.relationships.items() if r.kind == kind}


def relatives(ctx: VerifyContext, which: str, pid: str) -> Relatives:
    """readPersonSpouses / readPersonParents / readPersonChildren of ``pid``."""
    if which not in ("spouses", "parents", "children"):
        raise ValueError(f"unknown relatives {which!r}")
    result = getattr(ctx.api, f"read_person_{which}")(pid)
    if result.status == 204 or not (result.text or "").strip():
        return Relatives(frozenset(), {}, result)
    body = result.body if isinstance(result.body, dict) else {}
    persons = frozenset(str(p["id"]) for p in body.get("persons") or []
                        if isinstance(p, dict) and p.get("id"))
    # The live relatives reads were proven by ids only: an untyped entry in a
    # person's ``relationships`` is taken as a couple there (never on a relationship
    # resource, whose type a create recipe checks).
    return Relatives(persons, _relationships(result, untyped_couple=True), result)


def other_gender(gender_type: str | None) -> str:
    """The binary gender a person of ``gender_type`` is changed to."""
    return "Male" if gender_type == GENDER_URI["Female"] else "Female"
