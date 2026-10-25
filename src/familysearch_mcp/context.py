"""Shared state for the MCP server: config, auth, environment and write gates."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import requests
from urllib.parse import urlsplit

from familysearch.client import DEFAULT_USER_AGENT
from familysearch import (
    Discovery,
    Environment,
    FSClient,
    FSError,
    Memories,
    Search,
    Sources,
    TokenExpiredError,
    TreeReader,
    TreeWriter,
    detect_environment,
    extract_brave_token,
)
from familysearch.api import FamilySearchApi

_TRUTHY = {"1", "true", "yes"}


class ContextError(FSError):
    """Configuration, authentication, or write-gate problem."""


@dataclass(frozen=True)
class Services:
    client: FSClient
    discovery: Discovery
    tree: TreeReader
    writer: TreeWriter
    search: Search
    memories: Memories
    sources: Sources
    api: FamilySearchApi

    def __repr__(self) -> str:  # never expose the client (which holds the token)
        return "Services(...)"


def _status_of_current_user(
    token: str, seen: list[int] | None = None
) -> Callable[[str], int]:
    """Bootstrap probe for detect_environment: GET /platform/users/current.

    Sends the library's browser User-Agent (FamilySearch's edge answers 403 to
    python-requests) and records every status seen in `seen`.
    """

    def http_get(base_url: str) -> int:
        try:
            resp = requests.get(
                base_url + "/platform/users/current",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/x-fs-v1+json",
                    "User-Agent": DEFAULT_USER_AGENT,
                },
                timeout=30,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            host = urlsplit(base_url).netloc
            raise ContextError(
                f"could not reach FamilySearch at {host}: {type(exc).__name__}"
            ) from None
        if seen is not None:
            seen.append(resp.status_code)
        return resp.status_code

    return http_get


class Context:
    def __init__(self) -> None:
        raw = os.environ.get("FS_ENVIRONMENT", "production").strip().lower()
        try:
            self.environment = Environment[raw.upper()]
        except KeyError:
            raise ContextError(
                f"FS_ENVIRONMENT must be 'beta' or 'production', got {raw!r}."
            ) from None
        self._allow_prod_writes = (
            os.environ.get("FS_ALLOW_PROD_WRITES", "").strip().lower() in _TRUTHY
        )
        self._services: Services | None = None

    def __repr__(self) -> str:
        return f"Context(environment={self.environment.name.lower()})"

    def _build(self) -> Services:
        token = extract_brave_token()
        seen: list[int] = []
        try:
            actual = detect_environment(token, _status_of_current_user(token, seen))
        except ContextError:
            raise
        except FSError:
            if seen and all(code == 403 for code in seen):
                raise ContextError(
                    "FamilySearch BLOCKED the auth check (HTTP 403 from both "
                    "hosts). This is not an expired login: the request was "
                    "rejected at FamilySearch's edge, which requires a browser "
                    "User-Agent. Re-logging in will not help."
                ) from None
            raise
        if actual is not self.environment:
            raise ContextError(
                f"Brave is signed into {actual.name.lower()}, but FS_ENVIRONMENT is "
                f"{self.environment.name.lower()}. Log in to the "
                f"{self.environment.name.lower()} FamilySearch site in Brave, or set "
                f"FS_ENVIRONMENT={actual.name.lower()}."
            )
        client = FSClient(token, self.environment)
        discovery = Discovery(client)
        return Services(
            client=client,
            discovery=discovery,
            tree=TreeReader(client, discovery),
            writer=TreeWriter(client, discovery),
            search=Search(client, discovery),
            memories=Memories(client, discovery),
            sources=Sources(client, discovery),
            api=FamilySearchApi(client),
        )

    def services(self) -> Services:
        if self._services is None:
            self._services = self._build()
        return self._services

    def call(self, fn: Callable[[Services], Any]) -> Any:
        """Run fn(services); on an expired token re-lift it once and retry."""
        try:
            return fn(self.services())
        except TokenExpiredError:
            self._services = None
        env = self.environment.name.lower()
        try:
            return fn(self.services())
        except TokenExpiredError:
            self._services = None
            raise ContextError(
                f"FamilySearch rejected the session token even after re-reading it "
                f"from Brave. Please log in to the {env} FamilySearch site in Brave "
                f"again, then retry."
            ) from None

    def writes_allowed(self) -> bool:
        return self.environment is Environment.BETA or self._allow_prod_writes

    def require_writes(self) -> None:
        if not self.writes_allowed():
            raise ContextError(
                "Writes are disabled on production. Set FS_ALLOW_PROD_WRITES=1 to "
                "enable them, or use FS_ENVIRONMENT=beta."
            )
