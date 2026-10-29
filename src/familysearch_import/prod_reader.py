"""Read-only FamilySearch adapter for link verification. GET/HEAD only."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import requests

from familysearch.auth import Environment, extract_brave_token
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.errors import FSError, FSHTTPError
from familysearch.search import Search
from familysearch.tree import TreeReader

from .classify import living_status
from .linking import FsPerson, compare, _tokens
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
    def production(
        cls,
        *,
        token_fn: Callable[[], str] = extract_brave_token,
        http_get: Callable[[str, str], int] | None = None,
    ) -> "ReadOnlyFS":
        """Build from Brave's session; refuse unless the token is a production one."""
        token = token_fn()
        get = http_get or _status_get
        if get(Environment.PRODUCTION.base_url, token) != 200:
            raise FSError("Brave's FamilySearch session is not signed in to production.")
        return cls(FSClient(token, Environment.PRODUCTION))

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

    def search_candidates(
        self, person: Person, limit: int = 3, status: str | None = None
    ) -> list[FsPerson]:
        if living_status(person, status) != "deceased":
            raise ValueError("search is only allowed for deceased people")
        main = _tokens(_strip_parens(person.display_name))
        if len(main) < 2:
            return []
        given, surname = " ".join(main[:-1]), main[-1]
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
        headers={"Authorization": f"Bearer {token}", "Accept": "application/x-fs-v1+json"},
        timeout=30,
        allow_redirects=False,
    )
    return resp.status_code
