"""Read-only FamilySearch adapter for link verification. GET/HEAD only."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import requests

from familysearch.auth import Environment, extract_brave_token
from familysearch.client import DEFAULT_USER_AGENT, FSClient
from familysearch.discovery import Discovery
from familysearch.errors import FSError, FSHTTPError
from familysearch.search import Search
from familysearch.tree import TreeReader

from .classify import living_status
from .linking import FsPerson, compare, _parse_name
from .scan import Person

READ_METHODS = frozenset({"GET", "HEAD"})
MIN_INTERVAL = 0.3
MAX_TRIES = 4
BACKOFF_BASE = 1.0
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_ID_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{3,4}$")
_REDIRECTS = {301, 302, 303, 307, 308}


class ReadOnlyError(FSError):
    """A non-read HTTP request was attempted through the read-only adapter."""


class ReadFailed(FSError):
    """A memories/sources read failed; callers must not treat it as empty."""


@dataclass(frozen=True)
class SourceRef:
    title: str
    url: str | None


@dataclass(frozen=True)
class Relative:
    fs: FsPerson
    relationship: Literal["parent", "child", "spouse"]


def _title(item: dict) -> str:
    for t in item.get("titles") or []:
        if isinstance(t, dict) and isinstance(t.get("value"), str) and t["value"].strip():
            return t["value"].strip()
    return ""


def _year(text: Any) -> int | None:
    if not isinstance(text, str):
        return None
    m = _YEAR_RE.search(text)
    return int(m.group(1)) if m else None


def _fact_year(person: dict, kind: str) -> int | None:
    for f in person.get("facts") or []:
        if isinstance(f, dict) and str(f.get("type", "")).endswith("/" + kind):
            date = f.get("date")
            y = _year(date.get("original") if isinstance(date, dict) else None)
            if y:
                return y
    return None


def _name(person: dict) -> str:
    display = person.get("display")
    if isinstance(display, dict) and display.get("name"):
        return str(display["name"])
    for n in person.get("names") or []:
        for form in (n.get("nameForms") if isinstance(n, dict) else None) or []:
            if isinstance(form, dict) and form.get("fullText"):
                return str(form["fullText"])
    return ""


def fs_person_from_dict(person: dict, requested_id: str | None = None) -> FsPerson:
    """Map a GEDCOM X person to FsPerson (status ok, or redirected if the id differs)."""
    display = person.get("display") if isinstance(person.get("display"), dict) else {}
    pid = str(person.get("id") or requested_id or "")
    birth = _year(display.get("birthDate")) or _fact_year(person, "Birth")
    death = _year(display.get("deathDate")) or _fact_year(person, "Death")
    living = person.get("living")
    living = living if isinstance(living, bool) else None
    if requested_id and pid and pid != requested_id:
        return FsPerson(requested_id, _name(person), birth, death, living, "redirected", pid)
    return FsPerson(pid, _name(person), birth, death, living, "ok")


class ReadOnlyFS:
    def __init__(
        self,
        client: FSClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None
        self.last_response: requests.Response | None = None
        real_request = client.request

        def guarded(method, path_or_url, *args, **kwargs):
            if str(method).upper() not in READ_METHODS or kwargs.get("write"):
                raise ReadOnlyError(f"read-only adapter refuses {method!r} requests")
            resp = real_request(method, path_or_url, *args, **kwargs)
            self.last_response = resp
            return resp

        client.request = guarded  # type: ignore[method-assign]
        discovery = Discovery(client)
        self._tree = TreeReader(client, discovery)
        self._search = Search(client, discovery)

    @classmethod
    def for_environment(
        cls,
        env: Environment,
        *,
        token_fn: Callable[[], str] = extract_brave_token,
        http_get: Callable[[str, str], int] | None = None,
    ) -> "ReadOnlyFS":
        """Build from Brave's session; refuse unless the token belongs to env."""
        token = token_fn()
        get = http_get or _status_get
        if get(env.base_url, token) != 200:
            raise FSError(f"Brave's FamilySearch session is not signed in to {env.name.lower()}.")
        return cls(FSClient(token, env))

    @classmethod
    def production(cls, **kw) -> "ReadOnlyFS":
        return cls.for_environment(Environment.PRODUCTION, **kw)

    @classmethod
    def beta(cls, **kw) -> "ReadOnlyFS":
        return cls.for_environment(Environment.BETA, **kw)

    def _pace(self) -> None:
        if self._last_call is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _call(self, fn: Callable[[], Any]) -> Any:
        """Run fn with pacing; retry 429/5xx/network errors with backoff, then raise."""
        for attempt in range(MAX_TRIES):
            self._pace()
            try:
                return fn()
            except FSHTTPError as e:
                transient = e.status == 429 or e.status >= 500
                if not transient or attempt == MAX_TRIES - 1:
                    raise
            except requests.RequestException:
                if attempt == MAX_TRIES - 1:
                    raise
            self._sleep(BACKOFF_BASE * 2**attempt)
        raise AssertionError("unreachable")

    def get_fs_person(self, person_id: str, _depth: int = 0) -> FsPerson:
        def err() -> FsPerson:
            return FsPerson(person_id, "", None, None, None, "error")

        self.last_response = None
        try:
            data = self._call(lambda: self._tree.get_person(person_id))
        except FSHTTPError as e:
            if e.status == 404:
                return FsPerson(person_id, "", None, None, None, "not-found")
            if e.status == 410:
                return FsPerson(person_id, "", None, None, None, "deleted")
            return err()
        except FSError as e:
            if type(e) is not FSError:  # token / link errors are fatal, not per-person
                raise
            resp = self.last_response
            if resp is not None and resp.status_code in _REDIRECTS:
                survivor = (resp.headers.get("Location") or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                if _ID_RE.match(survivor) and survivor != person_id and _depth == 0:
                    sv = self.get_fs_person(survivor, _depth + 1)
                    if sv.status == "ok":
                        return FsPerson(person_id, sv.name, sv.birth_year, sv.death_year,
                                        sv.living, "redirected", survivor)
            return err()
        except requests.RequestException:
            return err()
        return fs_person_from_dict(data, person_id)

    def _read_list(self, person_id: str, fn: Callable[[], list[dict]]) -> list[dict]:
        """Run a list read with retries; any failure is ReadFailed (never an empty list)."""
        self.last_response = None
        try:
            return self._call(fn)
        except FSHTTPError as e:
            raise ReadFailed(f"read failed for {person_id}: HTTP {e.status}") from e
        except FSError as e:
            if type(e) is not FSError:  # token / link errors are fatal
                raise
            raise ReadFailed(f"read failed for {person_id}: {e}") from e
        except (requests.RequestException, ValueError) as e:
            raise ReadFailed(f"read failed for {person_id}: {type(e).__name__}") from e

    def get_relatives(self, person_id: str, strict: bool = False) -> list[Relative]:
        """Parents, children and spouses of a person; any failure is ReadFailed (never empty).
        ``strict``: a person without a parents/children/spouses link is ReadFailed, not empty."""
        self.last_response = None
        try:
            data = self._call(lambda: self._tree.get_relatives(person_id, strict=strict))
        except FSHTTPError as e:
            raise ReadFailed(f"read failed for {person_id}: HTTP {e.status}") from e
        except FSError as e:
            if type(e) is not FSError:  # token / link errors are fatal
                raise
            raise ReadFailed(f"read failed for {person_id}: {e}") from e
        except (requests.RequestException, ValueError) as e:
            raise ReadFailed(f"read failed for {person_id}: {type(e).__name__}") from e
        out: list[Relative] = []
        for section, rel in (("parents", "parent"), ("children", "child"), ("spouses", "spouse")):
            for p in data.get(section) or []:
                if not isinstance(p, dict) or not p.get("id") or p["id"] == person_id:
                    continue  # the spouses endpoint may echo the subject itself
                out.append(Relative(fs_person_from_dict(p), rel))
        return out

    def _get_json(self, url: str, what: str) -> dict | None:
        """GET a relationship resource: dict, or None when it is gone (404/410). Else ReadFailed."""
        self.last_response = None
        try:
            resp = self._call(lambda: self._client.get(url))
        except FSHTTPError as e:
            if e.status in (404, 410):
                return None
            raise ReadFailed(f"read failed for {what}: HTTP {e.status}") from e
        except FSError as e:
            if type(e) is not FSError:
                raise
            raise ReadFailed(f"read failed for {what}: {e}") from e
        except requests.RequestException as e:
            raise ReadFailed(f"read failed for {what}: {type(e).__name__}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise ReadFailed(f"read failed for {what}: not JSON") from e
        return data if isinstance(data, dict) else {}

    def get_couple(self, relationship_id: str) -> frozenset[str] | None:
        """The two person ids of a couple relationship; None when it does not exist."""
        from familysearch.tree import couple_url
        data = self._get_json(couple_url(self._client.base_url, relationship_id), "couple relationship")
        if data is None:
            return None
        rels = data.get("relationships")
        if not (isinstance(rels, list) and len(rels) == 1 and isinstance(rels[0], dict)):
            raise ReadFailed("read failed for couple relationship: unexpected shape")
        r = rels[0]
        ends = [(r.get(k) or {}).get("resourceId") for k in ("person1", "person2")]
        if not all(isinstance(x, str) and x for x in ends):
            raise ReadFailed("read failed for couple relationship: missing persons")
        return frozenset(ends)

    def get_child_and_parents(self, relationship_id: str) -> tuple[str, frozenset[str]] | None:
        """(child id, parent ids) of a child-and-parents relationship; None when it does not exist."""
        from familysearch.tree import child_and_parents_url
        data = self._get_json(child_and_parents_url(self._client.base_url, relationship_id),
                              "child-and-parents relationship")
        if data is None:
            return None
        rels = data.get("childAndParentsRelationships")
        if not (isinstance(rels, list) and len(rels) == 1 and isinstance(rels[0], dict)):
            raise ReadFailed("read failed for child-and-parents relationship: unexpected shape")
        r = rels[0]
        child = (r.get("child") or {}).get("resourceId")
        parents = [(r.get(k) or {}).get("resourceId") for k in ("parent1", "parent2") if r.get(k)]
        if not (isinstance(child, str) and child and parents and all(isinstance(x, str) and x for x in parents)):
            raise ReadFailed("read failed for child-and-parents relationship: missing persons")
        return child, frozenset(parents)

    def get_couple_ids(self, person_id: str, other_id: str) -> list[str]:
        """Couple relationship ids between two people (read-only)."""
        self.last_response = None
        try:
            return self._call(lambda: self._tree.get_couple_relationship_ids(person_id, other_id))
        except FSHTTPError as e:
            raise ReadFailed(f"read failed for {person_id}: HTTP {e.status}") from e
        except FSError as e:
            if type(e) is not FSError:
                raise
            raise ReadFailed(f"read failed for {person_id}: {e}") from e
        except (requests.RequestException, ValueError) as e:
            raise ReadFailed(f"read failed for {person_id}: {type(e).__name__}") from e

    def get_change_summary(self, person_id: str) -> list[tuple[str, str]]:
        """(contributor, operation text) per change-history entry; contributor "" when unknown."""
        import json as _json
        items = self._read_list(person_id, lambda: self._tree.get_changes_all(person_id))
        out = []
        for i in items:
            contribs = i.get("contributors") if isinstance(i.get("contributors"), list) else []
            who = ""
            for c in contribs:
                if isinstance(c, dict) and (c.get("resourceId") or c.get("name")):
                    who = str(c.get("resourceId") or c.get("name"))
                    break
            text = (_json.dumps(i.get("changeInfo"), sort_keys=True, default=str) + " "
                    + str(i.get("title") or "")).lower()
            out.append((who, text))
        return out

    def search_duplicates(self, given: str, surname: str, birth: int | None, death: int | None,
                          alt_surnames: Sequence[str] = ()) -> list[FsPerson]:
        """UNFILTERED tree search for anyone who might be the same person: given+surname and
        surname-only, each at birth and death years -10..+10 (steps of 5), for the surname and every
        alternative surname. Any entry returned is a hit (no plausibility filter). The response is parsed
        strictly: anything but 204 or a 200 feed raises ReadFailed (never an empty list for a failed search)."""
        surnames = list(dict.fromkeys([surname, *alt_surnames]))
        queries = []
        for kind, year in (("birth_date", birth), ("death_date", death)):
            if year is None:
                continue
            for off in (-10, -5, 0, 5, 10):
                for sn in surnames:
                    for g in (given, None):
                        queries.append({"given": g, "surname": sn, kind: str(year + off)})
        if not queries:
            raise ValueError("duplicate search needs a birth or death year")
        hits: dict[str, FsPerson] = {}
        for q in queries:
            try:
                entries = self._call(lambda q=q: self._search.tree_search_strict(**q))
            except FSHTTPError as e:
                raise ReadFailed(f"duplicate search failed: HTTP {e.status}") from e
            except FSError as e:
                if type(e) is not FSError:
                    raise
                raise ReadFailed(f"duplicate search failed: {e}") from e
            for entry in entries:
                fs = _entry_person(entry)
                if fs is None:
                    eid = entry.get("id") if isinstance(entry, dict) else None
                    fs = FsPerson(str(eid or "(unparsed)"), "", None, None, None, "error")
                hits.setdefault(fs.id, fs)
        return list(hits.values())

    def current_user_ids(self) -> frozenset[str]:
        """Ids that identify the signed-in user in change history (never printed)."""
        data = self._get_json(self._client.base_url + "/platform/users/current", "current user")
        users = (data or {}).get("users")
        if not (isinstance(users, list) and users and isinstance(users[0], dict)):
            raise ReadFailed("read failed for current user: unexpected shape")
        u = users[0]
        # the signed-in user's OWN ids only (CIS id, tree-person id, tree/agent id); never matched by name
        ids = frozenset(str(u[k]) for k in ("id", "personId", "treeUserId", "cisUserId") if u.get(k))
        if not ids:
            raise ReadFailed("read failed for current user: no id")
        return ids

    def get_couple_extras(self, relationship_id: str) -> dict:
        """What others may have added to a couple relationship: counts of facts/sources/notes and the
        full change history as (contributor, text). Fails closed (ReadFailed) when it cannot be read."""
        import json as _json
        from familysearch.tree import couple_url
        data = self._get_json(couple_url(self._client.base_url, relationship_id), "couple relationship")
        rels = (data or {}).get("relationships")
        if not (isinstance(rels, list) and len(rels) == 1 and isinstance(rels[0], dict)):
            raise ReadFailed("read failed for couple relationship: unexpected shape")
        r = rels[0]
        links = r.get("links") if isinstance(r.get("links"), dict) else {}
        href = (links.get("change-history") or {}).get("href") if isinstance(links.get("change-history"), dict) else None
        if not isinstance(href, str) or not href:
            raise ReadFailed("read failed for couple relationship: no change-history link")
        out_changes = []
        seen = set()
        while href:
            if href in seen or len(seen) >= 50:
                raise ReadFailed("read failed for couple relationship: change-history paging loop")
            seen.add(href)
            page = self._get_json(href, "couple change history")
            if page is None:
                raise ReadFailed("read failed for couple relationship: change history gone")
            for i in page.get("entries") or []:
                contribs = i.get("contributors") if isinstance(i.get("contributors"), list) else []
                who = ""
                for c in contribs:
                    if isinstance(c, dict) and (c.get("resourceId") or c.get("name")):
                        who = str(c.get("resourceId") or c.get("name"))
                        break
                out_changes.append((who, (_json.dumps(i.get("changeInfo"), sort_keys=True, default=str) + " "
                                          + str(i.get("title") or "")).lower()))
            nxt = page.get("links", {}).get("next") if isinstance(page.get("links"), dict) else None
            href = nxt.get("href") if isinstance(nxt, dict) else None
        return {"facts": len(r.get("facts") or []), "sources": len(r.get("sources") or []),
                "notes": len(r.get("notes") or []), "changes": out_changes}

    def get_memory_titles(self, person_id: str) -> list[str]:
        items = self._read_list(person_id, lambda: self._tree.get_memories(person_id))
        return [_title(i) for i in items]  # untitled memories stay in the list: counts must be complete

    def get_memory_ids(self, person_id: str) -> list[str]:
        items = self._read_list(person_id, lambda: self._tree.get_memories(person_id))
        return self._ids(person_id, items)

    def _ids(self, person_id: str, items: list[dict]) -> list[str]:
        ids = [str(i["id"]) for i in items if isinstance(i, dict) and i.get("id")]
        if len(ids) != len(items):  # an item without an id would make counts untrustworthy
            raise ReadFailed(f"read failed for {person_id}: an item has no id")
        return ids

    def get_source_ids(self, person_id: str) -> list[str]:
        items = self._read_list(person_id, lambda: self._tree.get_sources(person_id))
        return self._ids(person_id, items)

    def get_source_refs(self, person_id: str) -> list[SourceRef]:
        items = self._read_list(person_id, lambda: self._tree.get_sources(person_id))
        out = []
        for i in items:
            about = i.get("about")
            out.append(SourceRef(_title(i), about if isinstance(about, str) and about else None))
        return out

    def search_candidates(
        self, person: Person, limit: int = 3, status: str | None = None
    ) -> list[FsPerson]:
        if living_status(person, status) != "deceased":
            raise ValueError("search is only allowed for deceased people")
        parsed = _parse_name(person.display_name)
        if not parsed.given or not parsed.surname:
            return []
        given, surname = parsed.given, parsed.surname
        birth = str(person.birth) if person.birth else None
        entries = self._call(
            lambda: self._search.tree_search(given=given, surname=surname, birth_date=birth)
        )
        scored: list[tuple[int, int, FsPerson]] = []
        for i, entry in enumerate(entries):
            fs = _entry_person(entry)
            if fs is None:
                continue
            verdict = compare(person, fs)
            if verdict != "mismatch":
                scored.append((0 if verdict == "confirmed" else 1, i, fs))
        scored.sort(key=lambda t: t[:2])
        return [fs for _, _, fs in scored[:limit]]


def _strip_parens(name: str) -> str:
    return re.sub(r"\([^)]*\)", " ", name)


def _entry_person(entry: Any) -> FsPerson | None:
    if not isinstance(entry, dict):
        return None
    content = entry.get("content")
    gx = content.get("gedcomx") if isinstance(content, dict) else None
    persons = gx.get("persons") if isinstance(gx, dict) else None
    if not (isinstance(persons, list) and persons and isinstance(persons[0], dict)):
        return None
    p = dict(persons[0])
    p.setdefault("id", entry.get("id"))
    fs = fs_person_from_dict(p)
    return fs if fs.id else None


def _status_get(base_url: str, token: str) -> int:
    resp = requests.get(
        base_url + "/platform/users/current",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/x-fs-v1+json",
            "User-Agent": DEFAULT_USER_AGENT,  # FamilySearch's edge answers 403 without a browser UA
        },
        timeout=30,
        allow_redirects=False,
    )
    return resp.status_code
