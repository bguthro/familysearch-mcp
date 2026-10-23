"""Memories: document upload and linking to persons."""
from __future__ import annotations

from ._urls import join_path
from .client import FSClient
from .discovery import Discovery
from .tree import _new_id


class Memories:
    MAX_BYTES = 15 * 1024 * 1024
    ALLOWED_TYPES = {"image/png", "image/jpeg", "image/tiff", "application/pdf"}

    def __init__(self, client: FSClient, discovery: Discovery):
        self._client = client
        self._discovery = discovery

    def _artifacts(self) -> str:
        return self._discovery.href("artifacts")

    def upload(
        self, data: bytes, *, filename: str, content_type: str,
        title: str | None = None, reason: str,
    ) -> str:
        if len(data) > self.MAX_BYTES:
            raise ValueError(f"file is {len(data)} bytes; max is {self.MAX_BYTES}")
        if content_type not in self.ALLOWED_TYPES:
            raise ValueError(f"unsupported content type: {content_type}")
        params = {"filename": filename}
        if title:
            params["title"] = title
        # Multipart: let requests set Content-Type with the boundary (no JSON).
        resp = self._client.post(
            self._artifacts(),
            files={"artifact": (filename, data, content_type)},
            params=params,
            reason=reason,
        )
        return _new_id(resp)

    def link_to_person(self, person_id: str, memory_id: str, *, reason: str) -> None:
        url = join_path(self._artifacts(), memory_id, "personas")
        self._client.post(url, json={"persons": [{"id": person_id}]}, reason=reason)

    def delete(self, memory_id: str, *, reason: str) -> None:
        self._client.delete(join_path(self._artifacts(), memory_id), reason=reason)
