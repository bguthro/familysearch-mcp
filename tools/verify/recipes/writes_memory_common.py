"""Read-backs and bodies shared by the memory write recipes (P5).

Memories, their comments and personas, and a person's portraits are read back with
reads that are verified (``readMemory``, ``readMemoryPersonas``, ``readMemoryPersona``,
``getPersonMemories``) or, for ``readMemoryComments`` and ``readPersonPortraits``, not
yet verified until their own read recipes run live; the write recipes use all of them
unclaimed, as the P4 recipes used the source box reads. Failure text drops links,
attribution and contributors and redacts account ids (``describe_box``): a comment or
persona may name the signed-in user.

Request bodies not proven on beta before P5 (the spec has no examples) are built here
and in ``tools/verify/fixtures`` so a live correction is made once: ``memory_body``
(updateMemory), ``persona_name_body`` (updateMemoryPersona), ``persona_link_body`` (the
proven ``Memories.link_to_person`` body), ``comments_body`` and ``portrait_body``.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from familysearch.api._runtime import ApiResult
from familysearch.models import build_name

from ..core import VerifyContext
from .writes_common import Listing, describe_box, person_memories, poll, status_of

NO_SUCH_MEMORY = "999999999"  # memory ids are numeric (recon)
NOT_FOUND = (400, 404, 410)
TREE_PERSON = "http://familysearch.org/v1/FamilyTreePerson"
SURNAME = "ZZSPIKEUpdated"


def own(ctx: VerifyContext, kind: str, oid: str) -> None:
    """Refuse to write anything this run did not create and track."""
    if oid not in ctx.fixtures.tracked(kind):
        raise AssertionError(f"refusing to write {kind} {oid}: not created and tracked by "
                             "this run")


# ------------------------------------------------------------------ bodies


def memory_body(description_id: str, title: str, description: str) -> dict:
    """updateMemory: the memory's description by the id readMemory answers, with a new
    title and description (a GEDCOM X SourceDescription list; no example in the spec)."""
    return {"sourceDescriptions": [{"id": description_id, "titles": [{"value": title}],
                                    "descriptions": [{"value": description}]}]}


def persona_name_body(persona_id: str, given: str, identifiers: Any = None) -> dict:
    """updateMemoryPersona: the persona by id with a new name (parts, as beta requires
    for a person's names), keeping its identifiers (the tree link) when given. No
    example in the spec."""
    person: dict = {"id": persona_id, "names": [build_name(given, SURNAME)]}
    if identifiers:
        person["identifiers"] = identifiers
    return {"persons": [person]}


# ------------------------------------------------------------------ memory


@dataclass(frozen=True)
class MemoryView:
    """readMemory's list of artifact descriptions, flattened: every id, title, description,
    ``about`` and media type in the order answered."""

    ids: tuple
    titles: tuple
    descriptions: tuple
    about: tuple
    media: tuple
    result: ApiResult = field(compare=False, repr=False)


def _values(items: Any) -> tuple:
    return tuple(i.get("value") if isinstance(i, dict) else i
                 for i in (items if isinstance(items, list) else []))


def memory_view(result: ApiResult) -> MemoryView:
    body = result.body if isinstance(result.body, dict) else {}
    found = [d for d in body.get("sourceDescriptions") or [] if isinstance(d, dict)]
    if not found:
        raise AssertionError(f"readMemory answered no description; {describe_box(result)}")
    return MemoryView(
        ids=tuple(str(d.get("id")) for d in found),
        titles=tuple(v for d in found for v in _values(d.get("titles"))),
        descriptions=tuple(v for d in found for v in _values(d.get("descriptions"))),
        about=tuple(d.get("about") for d in found),
        media=tuple(d.get("mediaType") for d in found), result=result)


def read_memory(ctx: VerifyContext, mid: str) -> MemoryView:
    return memory_view(ctx.api.read_memory(mid))


def one_description_id(view: MemoryView, what: str) -> str:
    if len(view.ids) != 1 or view.ids[0] in ("", "None"):
        raise AssertionError(f"{what} reads back descriptions {list(view.ids)}, not exactly "
                             f"one with an id; {describe_box(view.result)}")
    return view.ids[0]


def poll_memory(ctx: VerifyContext, mid: str, done: Callable[[MemoryView], bool],
                what: str) -> MemoryView:
    return poll(ctx, lambda: read_memory(ctx, mid), done, what, shown=describe_box)


def poll_status_gone(ctx: VerifyContext, read: Callable[[], int], what: str) -> int:
    """Read a status until it is 404/410 (bounded); other statuses are not errors."""
    return poll(ctx, read, lambda s: s in (404, 410), what, result_of=lambda _s: None)


def unknown_memory_status(ctx: VerifyContext) -> int:
    return status_of(lambda: ctx.api.read_memory(NO_SUCH_MEMORY))


# ------------------------------------------------------------------ person memories


def poll_person_memories(ctx: VerifyContext, pid: str,
                         done: Callable[[Listing], bool], what: str) -> Listing:
    return poll(ctx, lambda: person_memories(ctx, pid), done, what, shown=describe_box)


def exactly(found: Listing, wanted: set[str], what: str) -> None:
    if set(found.items) != wanted:
        raise AssertionError(f"{what} holds {sorted(found.items)}, not exactly "
                             f"{sorted(wanted)}; {describe_box(found.result)}")


# ------------------------------------------------------------------ comments


def comments_listing(ctx: VerifyContext, mid: str) -> Listing:
    """readMemoryComments: comment id -> its text (``discussions[].comments[]``)."""
    result = ctx.api.read_memory_comments(mid)
    body = result.body if isinstance(result.body, dict) else {}
    items = {}
    for discussion in body.get("discussions") or []:
        for comment in (discussion.get("comments") or []) if isinstance(discussion, dict) \
                else []:
            if not (isinstance(comment, dict) and comment.get("id")):
                raise AssertionError(f"a listed comment has no id; {describe_box(result)}")
            items[str(comment["id"])] = comment.get("text")
    return Listing(items, result)


def poll_comments(ctx: VerifyContext, mid: str, done: Callable[[Listing], bool],
                  what: str) -> Listing:
    return poll(ctx, lambda: comments_listing(ctx, mid), done, what, shown=describe_box)


def comment_id(found: Listing, text: str) -> str:
    ids = [c for c, t in found.items.items() if t == text]
    if len(ids) != 1:
        raise AssertionError(f"expected exactly one comment reading {text!r}, found "
                             f"{len(ids)}; {describe_box(found.result)}")
    return ids[0]


# ------------------------------------------------------------------ personas


@dataclass(frozen=True)
class Persona:
    """A memory persona: its id, every name it carries and the JSON of its identifiers
    (which hold the tree person's id, with ``includeTreeIdentifier``)."""

    id: str
    names: frozenset
    identifiers: str

    def links(self, pids: Mapping[str, Any] | set | frozenset | list) -> tuple:
        return tuple(sorted(p for p in pids if p in self.identifiers))


def _persona(person: dict) -> Persona:
    names = set()
    display = person.get("display")
    if isinstance(display, dict) and isinstance(display.get("name"), str):
        names.add(display["name"])
    for name in person.get("names") or []:
        for form in (name.get("nameForms") or []) if isinstance(name, dict) else []:
            if isinstance(form, dict) and isinstance(form.get("fullText"), str):
                names.add(form["fullText"])
    return Persona(str(person.get("id")), frozenset(names),
                   json.dumps(person.get("identifiers"), sort_keys=True))


def personas_listing(ctx: VerifyContext, mid: str) -> Listing:
    """readMemoryPersonas (with tree identifiers): persona id -> ``Persona``; 204: none."""
    result = ctx.api.read_memory_personas(mid, query={"includeTreeIdentifier": True})
    if result.status == 204 or not (result.text or "").strip():
        return Listing({}, result)
    body = result.body if isinstance(result.body, dict) else {}
    items = {}
    for person in body.get("persons") or []:
        if not (isinstance(person, dict) and person.get("id")):
            raise AssertionError(f"a listed persona has no id; {describe_box(result)}")
        items[str(person["id"])] = _persona(person)
    return Listing(items, result)


def poll_personas(ctx: VerifyContext, mid: str, done: Callable[[Listing], bool],
                  what: str) -> Listing:
    return poll(ctx, lambda: personas_listing(ctx, mid), done, what, shown=describe_box)


def link_sets(found: Listing, pids: list[str]) -> list[tuple]:
    """Who each persona links to (among ``pids``), sorted: ``[(a,), (b,)]`` for two
    personas of two persons. A persona linking none of them shows as ``()``."""
    return sorted(p.links(pids) for p in found.items.values())


def persona_of(found: Listing, pid: str) -> str:
    """The id of the one persona linking ``pid``."""
    ids = [i for i, p in found.items.items() if pid in p.identifiers]
    if len(ids) != 1:
        raise AssertionError(f"expected exactly one persona of {pid}, found {len(ids)}; "
                             f"{describe_box(found.result)}")
    return ids[0]


def read_persona(ctx: VerifyContext, mid: str, persona_id: str) -> Persona:
    result = ctx.api.read_memory_persona(mid, persona_id)
    body = result.body if isinstance(result.body, dict) else {}
    found = [p for p in body.get("persons") or []
             if isinstance(p, dict) and p.get("id") == persona_id]
    if len(found) != 1:
        raise AssertionError(f"the read of persona {persona_id} does not hold it once; "
                             f"{describe_box(result)}")
    return _persona(found[0])


def poll_persona(ctx: VerifyContext, mid: str, persona_id: str,
                 done: Callable[[Persona], bool], what: str) -> Persona:
    """Read a persona until ``done``; a failure shows the names it last read (the
    persona read keeps no ApiResult)."""
    last: list[Persona] = []

    def fetch() -> Persona:
        last[:] = [read_persona(ctx, mid, persona_id)]
        return last[0]

    try:
        return poll(ctx, fetch, done, what, result_of=lambda _p: None)
    except AssertionError as e:
        names = sorted(last[0].names) if last else []
        raise AssertionError(f"{e}; the persona last read with names {names} and "
                             f"identifiers {last[0].identifiers if last else ''}") from None


def persona_link_body(memory_url: str, base_url: str, pid: str) -> dict:
    """createMemoryPersona linking an existing memory to a tree person: the body
    ``Memories.link_to_person`` proved on beta (the memory, and the person's ark)."""
    ark = f"{base_url}/ark:/61903/4:1:{quote(pid, safe='')}"
    return {"persons": [{"media": [{"description": memory_url}],
                         "identifiers": {TREE_PERSON: [ark]}}]}


# ------------------------------------------------------------------ portraits


def portraits_listing(ctx: VerifyContext, pid: str) -> Listing:
    """readPersonPortraits: description id -> its title values; 204 (none set): empty."""
    result = ctx.api.read_person_portraits(pid)
    if result.status == 204 or not (result.text or "").strip():
        return Listing({}, result)
    body = result.body if isinstance(result.body, dict) else {}
    return Listing({str(d.get("id")): _values(d.get("titles"))
                    for d in body.get("sourceDescriptions") or [] if isinstance(d, dict)},
                   result)


def portrait_titles(found: Listing) -> list:
    return sorted(t for titles in found.items.values() for t in titles)


def poll_portraits(ctx: VerifyContext, pid: str, done: Callable[[Listing], bool],
                   what: str) -> Listing:
    return poll(ctx, lambda: portraits_listing(ctx, pid), done, what, shown=describe_box)
