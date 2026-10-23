"""Tree reads: person, relatives, memories, sources, change history."""
from __future__ import annotations

from .client import FSClient
from .discovery import Discovery
from .errors import FSError


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

    def _fetch(self, links: dict[str, str], rel: str, *keys: str) -> list[dict]:
        href = links.get(rel)
        if not href:
            return []
        return _items(_json(self._client.get(href)), *keys)

    def get_relatives(self, person_id: str) -> dict:
        links = self._discovery.person_links(person_id)
        return {rel: self._fetch(links, rel, "persons") for rel in ("parents", "children", "spouses")}

    def get_memories(self, person_id: str) -> list[dict]:
        links = self._discovery.person_links(person_id)
        return self._fetch(links, "artifacts", "sourceDescriptions", "artifacts", "memories")

    def get_sources(self, person_id: str) -> list[dict]:
        links = self._discovery.person_links(person_id)
        return self._fetch(links, "source-descriptions", "sourceDescriptions")

    def get_changes(self, person_id: str) -> list[dict]:
        links = self._discovery.person_links(person_id)
        return self._fetch(links, "change-history", "entries")


GX = "http://gedcomx.org/"


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
        url = self._discovery.href("persons").rstrip("/")
        return url if url.startswith("http") else self._client.base_url + url

    def _ref(self, person_id: str) -> dict:
        return {"resource": f"{self._persons()}/{person_id}", "resourceId": person_id}

    def _post_relationship(self, rtype: str, p1: str, p2: str, reason: str) -> str:
        body = {"relationships": [{"type": GX + rtype, "person1": self._ref(p1), "person2": self._ref(p2)}]}
        resp = self._client.post(self._discovery.href("relationships"), json=body, reason=reason)
        return _new_id(resp)

    def create_person(self, person: dict, *, reason: str) -> str:
        resp = self._client.post(self._persons(), json={"persons": [person]}, reason=reason)
        return _new_id(resp)

    def add_fact(self, person_id: str, fact: dict, *, reason: str) -> None:
        self._client.post(
            f"{self._persons()}/{person_id}",
            json={"persons": [{"facts": [fact]}]},
            reason=reason,
        )

    def create_couple(self, person1_id: str, person2_id: str, *, reason: str) -> str:
        return self._post_relationship("Couple", person1_id, person2_id, reason)

    def create_parent_child(self, *, child_id: str, parent_ids: list[str], reason: str) -> list[str]:
        return [self._post_relationship("ParentChild", p, child_id, reason) for p in parent_ids]

    def delete_person(self, person_id: str, *, reason: str) -> None:
        self._client.delete(f"{self._persons()}/{person_id}", reason=reason)
