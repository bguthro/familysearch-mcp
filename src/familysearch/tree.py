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
