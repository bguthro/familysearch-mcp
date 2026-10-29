"""Read-backs shared by the Genealogies (private tree) recipes, and their guarded shapes.

The private-tree API is unproven on beta, so every parser here is tolerant of the
envelope (it looks for GEDCOM X ``collections`` / ``sourceDescriptions`` / ``persons``) and
fails with the response skeleton (``describe_box``: links and attribution dropped, account
ids redacted) when what it needs is absent, so a live failure shows the real shape.

The reads used here without being claimed by a write recipe: readGenealogiesTree, readGenealogiesPersons, readGenealogiesPerson
and readGenealogiesSourceDescription (verified by the ``reads.genealogies.*`` cases).
Ids are used exactly as the server returns them: no recipe builds or assumes a prefix.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from familysearch.api._runtime import ApiResult

from ..core import VerifyContext
from ..fixtures import entry_title, same_id, tree_entries
from .writes_common import (Listing, PersonView, Relationship, _relationship, describe_box,
                            person_view)

# A well-formed unknown id: live, created ids came back as ``2:2:2:<id>`` (the prefix the
# recon's 400 "Invalid prefix for genealogies" asked for). Recipes never build a prefix;
# only this static control id carries it.
NO_SUCH_GENEALOGIES = "2:2:2:ZZZZ-ZZZ"


def shown(entry: dict) -> str:
    """What a tree entry shows of itself: its title, else its whole JSON."""
    return entry_title(entry) or json.dumps(entry, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class TreeView:
    id: str
    shown: str
    result: ApiResult = field(compare=False, repr=False)


def read_tree(ctx: VerifyContext, tid: str) -> TreeView:
    result = ctx.api.read_genealogies_tree(tid)
    entries = tree_entries(result.body)
    found = [e for i, e in entries.items() if same_id(i, tid)]
    if len(found) != 1:
        raise AssertionError(f"the read of tree {tid} does not hold it; {describe_box(result)}")
    return TreeView(tid, shown(found[0]), result)


def _name_of(person: dict) -> str:
    names = person.get("names") or []
    forms = (names[0].get("nameForms") if names and isinstance(names[0], dict) else None) or [{}]
    return (forms[0] or {}).get("fullText") or ""


def persons(ctx: VerifyContext, tid: str) -> Listing:
    """readGenealogiesPersons: the tree's persons, id -> (full name, living). 204: none."""
    result = ctx.api.read_genealogies_persons(tid)
    if result.status == 204 or not (result.text or "").strip():
        return Listing({}, result)
    body = result.body if isinstance(result.body, dict) else {}
    items = {}
    for person in body.get("persons") or []:
        if isinstance(person, dict) and person.get("id"):
            items[str(person["id"])] = (_name_of(person), person.get("living"))
    if not items:
        raise AssertionError(f"the persons of tree {tid} are not in persons; "
                             f"{describe_box(result)}")
    return Listing(items, result)


@dataclass(frozen=True)
class PrivatePerson:
    """readGenealogiesPerson: the conclusions (as readPerson's view) and the living flag."""

    view: PersonView
    living: Any
    relationships: Any = field(compare=False, default=None)
    result: ApiResult = field(compare=False, repr=False, default=None)


def read_person(ctx: VerifyContext, pid: str) -> PrivatePerson:
    result = ctx.api.read_genealogies_person(pid)
    view = person_view(result)  # its failures say readPerson: the same GEDCOM X shape
    person = result.body["persons"][0]
    if person.get("id") not in (None, pid):
        raise AssertionError(f"the read of person {pid} answered {person.get('id')}; "
                             f"{describe_box(result)}")
    return PrivatePerson(view, person.get("living"), person.get("relationships"), result)


def person_relationships(person: PrivatePerson) -> dict[str, Relationship]:
    """The relationships a person read lists (``relationships`` entries): id -> view.

    Whether beta lists them there is unproven: an absent key reads as none, and the
    relationship recipes fail with the person's skeleton if they expect one.
    """
    found = {}
    for record in person.relationships or []:
        if isinstance(record, dict) and record.get("id"):
            found[str(record["id"])] = _relationship(record, "relationships", person.result)
    return found
