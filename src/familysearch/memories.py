"""Memories: document upload and linking to persons."""
from __future__ import annotations

from urllib.parse import quote

from ._urls import join_path
from .client import FSClient
from .discovery import Discovery
from .errors import LinkNotFoundError
from .tree import _new_id


class Memories:
    MAX_BYTES = 15 * 1024 * 1024
    ALLOWED_TYPES = {"image/png", "image/jpeg", "image/tiff", "application/pdf"}

    def __init__(self, client: FSClient, discovery: Discovery):
        self._client = client
        self._discovery = discovery

    def _artifacts(self) -> str:
        return self._discovery.href("artifacts")

    def _check(self, data: bytes, content_type: str) -> None:
        if len(data) > self.MAX_BYTES:
            raise ValueError(f"file is {len(data)} bytes; max is {self.MAX_BYTES}")
        if content_type not in self.ALLOWED_TYPES:
            raise ValueError(f"unsupported content type: {content_type}")

    def _post_multipart(self, url, data, filename, content_type, title, reason) -> str:
        params = {"filename": filename}
        if title:
            params["title"] = title
        # Multipart: let requests set Content-Type with the boundary (no JSON).
        resp = self._client.post(
            url, files={"artifact": (filename, data, content_type)}, params=params, reason=reason,
        )
        return _new_id(resp)

    def upload(
        self, data: bytes, *, filename: str, content_type: str,
        title: str | None = None, reason: str,
    ) -> str:
        """Upload a standalone ("floating") memory, attached to nobody."""
        self._check(data, content_type)
        return self._post_multipart(self._artifacts(), data, filename, content_type, title, reason)

    def upload_to_person(
        self, person_id: str, data: bytes, *, filename: str, content_type: str,
        title: str | None = None, reason: str,
    ) -> str:
        """Upload AND attach in one step, via the person's own memories link."""
        self._check(data, content_type)
        url = self._discovery.person_links(person_id).get("artifacts")
        if not url:
            raise LinkNotFoundError("artifacts")
        return self._post_multipart(url, data, filename, content_type, title, reason)

    def link_to_person(self, person_id: str, memory_id: str, *, reason: str) -> None:
        """Attach an EXISTING memory to a person (creates a persona; read-back proven)."""
        memory_url = join_path(self._artifacts().split("?")[0], memory_id)
        ark = f"{self._client.base_url}/ark:/61903/4:1:{quote(person_id, safe='')}"
        body = {"persons": [{
            "media": [{"description": memory_url}],
            "identifiers": {"http://familysearch.org/v1/FamilyTreePerson": [ark]},
        }]}
        self._client.post(memory_url + "/personas", json=body, reason=reason)

    def delete(self, memory_id: str, *, reason: str) -> None:
        self._client.delete(join_path(self._artifacts(), memory_id), reason=reason)
