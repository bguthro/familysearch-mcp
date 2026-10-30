"""Tree reads: person, relatives, memories, sources, change history."""
from __future__ import annotations

from ._urls import join_path
from .client import FSClient
from .discovery import Discovery
from .errors import FSError
from .search import ATOM_ACCEPT


def _json(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _items(body: dict, *keys: str) -> list[dict]:
    """First non-empty list found under any of keys; [] when absent."""
    for key in keys:
        val = body.get(key)
        if isinstance(val, list) and val:
            return [v for v in val if isinstance(v, dict)]
    return []


MAX_PAGES = 50


def _next_href(body: dict) -> str | None:
    """The GedcomX paging link (links.next.href, or a rel=next entry in a links list)."""
    links = body.get("links")
    if isinstance(links, dict):
        nxt = links.get("next")
        href = nxt.get("href") if isinstance(nxt, dict) else None
        return href if isinstance(href, str) and href else None
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("rel") == "next" and isinstance(link.get("href"), str):
                return link["href"] or None
    return None


class TreeReader:
    def __init__(self, client: FSClient, discovery: Discovery):
        self._client = client
        self._discovery = discovery

    def get_person(self, person_id: str) -> dict:
        body = _json(self._client.get(f"/platform/tree/persons/{person_id}"))
        persons = body.get("persons")
        if isinstance(persons, list) and persons and isinstance(persons[0], dict):
            return persons[0]
        raise FSError(f"person not found: {person_id}")

    def _fetch(self, links: dict[str, str], rel: str, *keys: str, accept=None) -> list[dict]:
        href = links.get(rel)
        if not href:
            return []
        return _items(_json(self._client.get(href, accept=accept)), *keys)

    def _fetch_all(self, links: dict[str, str], rel: str, *keys: str) -> list[dict]:
        """Like _fetch but follows paging links until exhausted. Never returns a truncated list:
        more than MAX_PAGES pages, or a paging loop, raises FSError."""
        href = links.get(rel)
        out: list[dict] = []
        seen: set[str] = set()
        pages = 0
        while href:
            if href in seen:
                raise FSError("paging loop while reading a list")
            seen.add(href)
            pages += 1
            if pages > MAX_PAGES:
                raise FSError(f"list has more than {MAX_PAGES} pages; refusing to treat it as complete")
            body = _json(self._client.get(href))
            out.extend(_items(body, *keys))
            href = _next_href(body)
        return out

    def get_relatives(self, person_id: str) -> dict:
        links = self._discovery.person_links(person_id)
        return {rel: self._fetch(links, rel, "persons") for rel in ("parents", "children", "spouses")}

    def get_memories(self, person_id: str) -> list[dict]:
        links = self._discovery.person_links(person_id)
        return self._fetch_all(links, "artifacts", "sourceDescriptions", "artifacts", "memories")

    def get_sources(self, person_id: str) -> list[dict]:
        links = self._discovery.person_links(person_id)
        return self._fetch_all(links, "source-descriptions", "sourceDescriptions")

    def get_changes(self, person_id: str) -> list[dict]:
        links = self._discovery.person_links(person_id)
        return self._fetch(links, "change-history", "entries")

    def get_matches(self, person_id: str) -> list[dict]:
        links = self._discovery.person_links(person_id)
        # The matches endpoint answers 406 to x-fs-v1+json; it needs the atom Accept
        # (204 with no body when there are no matches).
        return self._fetch(links, "matches", "entries", accept=ATOM_ACCEPT)


GX = "http://gedcomx.org/"
# Not a discovery rel: learned from the Location header of a child-and-parents POST.
# Likewise the couple resource (the `relationships` rel is only the creation collection;
# DELETE/GET there is 404): learned from the Location header of a couple POST.
COUPLE_PATH = "/platform/tree/couple-relationships"
CHILD_AND_PARENTS_PATH = "/platform/tree/child-and-parents-relationships"


def couple_url(base_url: str, relationship_id: str) -> str:
    """The couple relationship resource (GET and DELETE)."""
    return join_path(base_url + COUPLE_PATH, relationship_id)


def child_and_parents_url(base_url: str, relationship_id: str) -> str:
    """The child-and-parents relationship resource (GET and DELETE)."""
    return join_path(base_url + CHILD_AND_PARENTS_PATH, relationship_id)


def _new_id(resp) -> str:
    headers = getattr(resp, "headers", None) or {}
    eid = headers.get("X-ENTITY-ID")
    if eid:
        return eid
    loc = headers.get("Location") or ""
    seg = loc.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    if not seg:
        raise FSError("write succeeded but no new id was returned")
    return seg


class TreeWriter:
    def __init__(self, client: FSClient, discovery: Discovery):
        self._client = client
        self._discovery = discovery

    def _persons(self) -> str:
        url = self._discovery.href("persons")
        return url if url.startswith("http") else self._client.base_url + url

    def _ref(self, person_id: str) -> dict:
        return {"resource": join_path(self._persons().split("?")[0], person_id), "resourceId": person_id}

    def create_couple(self, person1_id: str, person2_id: str, *, reason: str) -> str:
        body = {"relationships": [{
            "type": GX + "Couple", "person1": self._ref(person1_id), "person2": self._ref(person2_id),
        }]}
        resp = self._client.post(self._discovery.href("relationships"), json=body, reason=reason)
        return _new_id(resp)

    def create_person(self, person: dict, *, reason: str) -> str:
        resp = self._client.post(self._persons(), json={"persons": [person]}, reason=reason)
        return _new_id(resp)

    def add_fact(self, person_id: str, fact: dict, *, reason: str) -> None:
        self._client.post(
            join_path(self._persons(), person_id),
            json={"persons": [{"facts": [fact]}]},
            reason=reason,
        )

    def create_parent_child(self, *, child_id: str, parent_ids: list[str], reason: str) -> str:
        """Create ONE child-and-parents relationship holding all given parents.

        (Posting ParentChild ``relationships`` creates couple links instead; proven live.)
        FamilySearch derives the parents' couple link from a two-parent relationship.
        """
        if not isinstance(parent_ids, (list, tuple)) or not 1 <= len(parent_ids) <= 2:
            raise ValueError("parent_ids must hold 1 or 2 person ids")
        rel = {f"parent{i}": self._ref(p) for i, p in enumerate(parent_ids, 1)}
        rel["child"] = self._ref(child_id)
        resp = self._client.post(
            self._discovery.href("relationships"),
            json={"childAndParentsRelationships": [rel]}, reason=reason,
        )
        return _new_id(resp)

    def delete_couple(self, relationship_id: str, *, reason: str) -> None:
        url = couple_url(self._client.base_url, relationship_id)
        self._client.delete(url, reason=reason)

    def delete_child_and_parents(self, relationship_id: str, *, reason: str) -> None:
        url = child_and_parents_url(self._client.base_url, relationship_id)
        self._client.delete(url, reason=reason)

    def delete_person(self, person_id: str, *, reason: str) -> None:
        self._client.delete(join_path(self._persons(), person_id), reason=reason)
