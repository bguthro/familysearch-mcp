from urllib.parse import parse_qs, urlparse

import pytest
import responses

from familysearch.search import normalize_search_date
from test_search import BASE, _load, _search


@pytest.mark.parametrize("raw,expected", [
    ("1846", "1846"),
    ("1846-12-25", "1846-12-25"),
    ("25 December 1846", "1846"),
    ("December 1846", "1846"),
    ("abt 1850", "1850"),
    ("about 1846", "1846"),
    ("after 1850", None),
    ("Before 1850", None),
    ("from 1840", None),
    ("until 1850", None),
    ("since 1840", None),
    ("1850s", None),
    ("the 1850s", None),
    ("circa 1850", "1850"),
    ("ca. 1850", "1850"),
    ("est 1850", "1850"),
    ("c. 1850", "1850"),
    ("Toronto 1850", "1850"),
    ("\uff11\uff18\uff14\uff16", None),
    ("\u0661\u0668\u0664\u0666", None),
    ("1846-13-45", "1846"),
    ("1846-02-30", "1846"),
    ("  1846  ", "1846"),
    ("30 March 1828", "1828"),
    ("1850 or 1850", "1850"),
    ("Between 1840 and 1850", None),
    ("1840-1850", None),
    ("junk", None),
    ("Lot 12345", None),
    ("0999", None),
    ("3000", None),
    ("", None),
    ("   ", None),
    (None, None),
    (1846, None),
    (["1846"], None),
])
def test_normalize_search_date(raw, expected):
    assert normalize_search_date(raw) == expected


def test_normalize_search_date_huge_junk_is_fast():
    import time
    t = time.monotonic()
    assert normalize_search_date("x1 " * 350000) is None
    assert normalize_search_date("9" * 1_000_000) is None
    assert time.monotonic() - t < 2


@responses.activate
@pytest.mark.parametrize("raw,expected", [
    ("1846", "1846"),
    ("1846-12-25", "1846-12-25"),
    ("25 December 1846", "1846"),
    ("December 1846", "1846"),
    ("abt 1846", "1846"),
    ("about 1846", "1846"),
    ("Between 1840 and 1850", None),
    ("junk", None),
    ("", None),
    (None, None),
    (1846, None),
])
def test_tree_search_outbound_birth_like_date(raw, expected):
    s = _search()
    responses.add(responses.GET, BASE + "/platform/tree/search", json=_load("tree_search.json"))
    s.tree_search(given="John", surname="Smith", birth_date=raw, birth_place="Ontario, Canada")
    q = parse_qs(urlparse(responses.calls[-1].request.url).query)
    if expected is None:
        assert "q.birthLikeDate" not in q
    else:
        assert q["q.birthLikeDate"] == [expected]
    assert q["q.birthLikePlace"] == ["Ontario, Canada"]
    assert q["q.givenName"] == ["John"] and q["q.surname"] == ["Smith"]
