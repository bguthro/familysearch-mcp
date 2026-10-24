"""Read-only MCP tools: thin wrappers over the familysearch client library.

Errors from the library or Context are deliberately not caught here; the MCP
layer turns a raised exception into an error result.
"""

from __future__ import annotations

from typing import Any

from .projections import (
    project_memory,
    project_person,
    project_record_hit,
    project_search_hit,
    project_source,
)
from .validation import clamp, validate_person_id


def _user_whitelist(body: Any) -> dict:
    """Only id, displayName and personId; never forward other user fields."""
    users = body.get("users") if isinstance(body, dict) else None
    user = users[0] if isinstance(users, list) and users else None
    user = user if isinstance(user, dict) else {}
    return {k: user.get(k) for k in ("id", "displayName", "personId")}


def _env_name(env: Any) -> str:
    return str(getattr(env, "name", env)).lower()


def register_read_tools(mcp: Any, ctx: Any) -> None:
    @mcp.tool()
    def auth_status() -> dict:
        """Report the FamilySearch environment ('production' or 'beta'), the signed-in
        user (id, displayName, personId), and whether write tools are enabled."""

        def run(s):
            body = s.client.get(s.discovery.href("current-user")).json()
            return {
                "environment": _env_name(ctx.environment),
                "user": _user_whitelist(body),
                "writes_allowed": ctx.writes_allowed(),
            }

        return ctx.call(run)

    @mcp.tool()
    def search_tree(
        given: str | None = None,
        surname: str | None = None,
        birth_date: str | None = None,
        birth_place: str | None = None,
        count: int = 20,
    ) -> list[dict]:
        """Search the FamilySearch Family Tree for persons by name and optional birth
        date/place. Returns up to `count` (1-50) hits with id, confidence, name,
        birth, death and gender."""
        n = clamp(count, 1, 50)
        return ctx.call(
            lambda s: [
                project_search_hit(e)
                for e in s.search.tree_search(
                    given=given, surname=surname, birth_date=birth_date,
                    birth_place=birth_place, count=n,
                )
            ]
        )

    @mcp.tool()
    def search_records(
        given: str | None = None,
        surname: str | None = None,
        count: int = 20,
        offset: int = 0,
    ) -> dict:
        """Search FamilySearch historical records by name. Returns {total, next,
        entries}; each entry has id, confidence, name and facts. Page with `offset`;
        `count` is limited to 1-50."""
        n = clamp(count, 1, 50)
        o = clamp(offset, 0, 10**9)

        def run(s):
            res = s.search.record_search(given=given, surname=surname, count=n, offset=o)
            return {
                "total": res.get("total"),
                "next": res.get("next"),
                "entries": [project_record_hit(e) for e in res.get("entries") or []],
            }

        return ctx.call(run)

    @mcp.tool()
    def get_person(person_id: str) -> dict:
        """Get a Family Tree person by id (like 'GKLV-DY8'): name, lifespan, gender,
        living flag, facts and a summary of available links."""
        pid = validate_person_id(person_id)
        return ctx.call(lambda s: project_person(s.tree.get_person(pid)))

    @mcp.tool()
    def get_relatives(person_id: str) -> dict:
        """Get a person's immediate family by id: {parents, children, spouses}, each
        a list of compact person records."""
        pid = validate_person_id(person_id)

        def run(s):
            rel = s.tree.get_relatives(pid)
            return {
                k: [project_person(p) for p in rel.get(k) or []]
                for k in ("parents", "children", "spouses")
            }

        return ctx.call(run)

    @mcp.tool()
    def get_person_sources(person_id: str) -> list[dict]:
        """List the sources attached to a Family Tree person (id, title, citation,
        about)."""
        pid = validate_person_id(person_id)
        return ctx.call(lambda s: [project_source(d) for d in s.tree.get_sources(pid)])

    @mcp.tool()
    def get_person_memories(person_id: str) -> list[dict]:
        """List the memories (photos, documents, stories) attached to a Family Tree
        person (id, title, description, link)."""
        pid = validate_person_id(person_id)
        return ctx.call(lambda s: [project_memory(m) for m in s.tree.get_memories(pid)])

    @mcp.tool()
    def get_matches(person_id: str) -> list[dict]:
        """List FamilySearch's suggested record/tree matches for a Family Tree person,
        as search hits (id, confidence, name, birth, death, gender)."""
        pid = validate_person_id(person_id)
        return ctx.call(lambda s: [project_search_hit(e) for e in s.tree.get_matches(pid)])
