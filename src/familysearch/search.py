"""Tree search and historical-record (persona) search."""
from __future__ import annotations

import datetime
import re

from .client import FSClient
from .discovery import Discovery

RECORD_SEARCH_PATH = "/service/search/hr/v2/personas"
ATOM_ACCEPT = "application/x-gedcomx-atom+json"
# The personas service rejects application/x-fs-v1+json with 406.
RECORD_ACCEPT = "application/json"


def _dict(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _entries(data: dict) -> list:
    entries = data.get("entries")
    return entries if isinstance(entries, list) else []


_BARE_YEAR = re.compile(r"\d{4}", re.ASCII)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_YEAR_RUN = re.compile(r"(?<!\d)\d{4}(?!\d)", re.ASCII)
# Bounds, ranges and decades NARROW a search (or are not one date): omit the date.
_NARROWING = re.compile(
    r"\b(?:after|aft|before|bef|from|to|until|since|between)\b|\d{4}s\b",
    re.ASCII | re.IGNORECASE,
)


def normalize_search_date(text) -> str | None:
    """Reduce a free-form date to what q.birthLikeDate accepts, or None to omit it.

    The search endpoint accepts ONLY a bare year or a valid ISO date (anything else is
    HTTP 400). A bare year or valid ISO date passes through. Text with a bound/range
    qualifier (after, before, from, to, until, since, between) or a decade ("1850s") is
    omitted, since a single year would NARROW the search. Otherwise (including
    approximations such as "abt 1850") a single distinct plausible year (1000-2999) is
    used; zero or several years omit the date.
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if _BARE_YEAR.fullmatch(s) and 1000 <= int(s) <= 2999:
        return s
    if _ISO_DATE.fullmatch(s):
        try:
            datetime.date.fromisoformat(s)
            return s
        except ValueError:
            pass
    if _NARROWING.search(s):
        return None
    years = {y for y in _YEAR_RUN.findall(s) if 1000 <= int(y) <= 2999}
    return years.pop() if len(years) == 1 else None


def _clean(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None}


class Search:
    def __init__(self, client: FSClient, discovery: Discovery):
        self._client = client
        self._discovery = discovery

    def tree_search(
        self, *, given=None, surname=None, birth_date=None, birth_place=None,
        count: int = 20, death_date=None,
    ) -> list[dict]:
        # The rel is a URI template (".../search?flag=fst{&access_token}"); drop the template part.
        url = self._discovery.href("person-search").split("{", 1)[0]
        params = _clean({
            "q.givenName": given, "q.surname": surname,
            "q.birthLikeDate": normalize_search_date(birth_date), "q.birthLikePlace": birth_place,
            "q.deathLikeDate": normalize_search_date(death_date),
            "count": count,
        })
        resp = self._client.get(url, accept=ATOM_ACCEPT, params=params)
        return _entries(_dict(resp))

    def record_search(
        self, *, given=None, surname=None, count: int = 20, offset: int = 0
    ) -> dict:
        params = _clean({"q.givenName": given, "q.surname": surname,
                         "count": count, "offset": offset})
        data = _dict(self._client.get(RECORD_SEARCH_PATH, accept=RECORD_ACCEPT, params=params))
        total = data.get("results")
        links = data.get("links")
        nxt = links.get("next") if isinstance(links, dict) else None
        href = nxt.get("href") if isinstance(nxt, dict) else None
        return {
            "total": total if isinstance(total, int) else 0,
            "entries": _entries(data),
            "next": href,
        }
