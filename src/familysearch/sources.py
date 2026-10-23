"""Sources: create source descriptions and attach them to persons."""
from __future__ import annotations

from ._urls import join_path
from .client import FSClient
from .discovery import Discovery
from .tree import _new_id


class Sources:
    def __init__(self, client: FSClient, discovery: Discovery):
        self._client = client
        self._discovery = discovery

    def create_description(self, about_url: str, title: str, citation: str, *, reason: str) -> str:
        body = {"sourceDescriptions": [{
            "about": about_url,
            "titles": [{"value": title}],
            "citations": [{"value": citation}],
        }]}
        resp = self._client.post(
            self._discovery.href("source-descriptions"), json=body, reason=reason
        )
        return _new_id(resp)

    def attach_to_person(self, person_id: str, source_description_url: str, *, reason: str) -> None:
        # Post to the person resource itself; /sources is GET-only (405).
        url = join_path(self._discovery.href('persons'), person_id)
        body = {"persons": [{"sources": [{"description": source_description_url}]}]}
        self._client.post(url, json=body, reason=reason)
