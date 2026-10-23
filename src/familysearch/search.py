"""Tree search and historical-record (persona) search."""
from __future__ import annotations

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


def _clean(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None}


class Search:
    def __init__(self, client: FSClient, discovery: Discovery):
        self._client = client
        self._discovery = discovery

    def tree_search(
        self, *, given=None, surname=None, birth_date=None, birth_place=None,
        count: int = 20,
    ) -> list[dict]:
        # The rel is a URI template (".../search?flag=fst{&access_token}"); drop the template part.
        url = self._discovery.href("person-search").split("{", 1)[0]
        params = _clean({
            "q.givenName": given, "q.surname": surname,
            "q.birthLikeDate": birth_date, "q.birthLikePlace": birth_place,
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
