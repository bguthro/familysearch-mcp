"""HATEOAS discovery: resolve endpoints from link rels rather than hardcoding."""
from __future__ import annotations

from .client import FSClient
from .errors import LinkNotFoundError

TREE_COLLECTION = "/platform/collections/tree"


def _flatten(links) -> dict[str, str]:
    """Map rel -> href (or template) from a GEDCOM X links object."""
    out: dict[str, str] = {}
    if not isinstance(links, dict):
        return out
    for rel, val in links.items():
        if isinstance(val, dict):
            target = val.get("href") or val.get("template")
            if target:
                out[rel] = target
    return out


def _body(client: FSClient, path: str) -> dict:
    data = client.get(path).json()
    return data if isinstance(data, dict) else {}


class Discovery:
    def __init__(self, client: FSClient):
        self._client = client
        self._links: dict[str, str] | None = None

    def links(self) -> dict[str, str]:
        if self._links is None:
            data = _body(self._client, TREE_COLLECTION)
            merged = _flatten(data.get("links"))
            cols = data.get("collections")
            if isinstance(cols, list) and cols and isinstance(cols[0], dict):
                merged.update(_flatten(cols[0].get("links")))
            self._links = merged
        return self._links

    def href(self, rel: str) -> str:
        try:
            return self.links()[rel]
        except KeyError:
            raise LinkNotFoundError(f"link rel not found: {rel}") from None

    def person_links(self, person_id: str) -> dict[str, str]:
        data = _body(self._client, f"/platform/tree/persons/{person_id}")
        persons = data.get("persons")
        if isinstance(persons, list) and persons and isinstance(persons[0], dict):
            return _flatten(persons[0].get("links"))
        return {}
