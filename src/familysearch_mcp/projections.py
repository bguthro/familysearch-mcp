"""Pure projections: raw GEDCOM X dicts -> compact dicts for an LLM client.

No I/O. Every function tolerates missing keys and wrong types and never raises.
"""

from __future__ import annotations

from typing import Any

_GEDCOMX = "http://gedcomx.org/"


def _d(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list:
    return v if isinstance(v, list) else []


def _s(v: Any) -> Any:
    """Return v if it is a string, else None."""
    return v if isinstance(v, str) else None


def _id(v: Any) -> str | None:
    """Ids may arrive as JSON integers; keep them as strings. Bool/empty -> None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    return v if isinstance(v, str) and v else None


def _first(v: Any) -> dict:
    items = _l(v)
    return _d(items[0]) if items else {}


def _short(t: Any) -> str | None:
    t = _s(t)
    if t is None:
        return None
    return t[len(_GEDCOMX):] if t.startswith(_GEDCOMX) else t


def _fact(f: Any) -> dict:
    f = _d(f)
    date = _d(f.get("date"))
    return {
        "type": _short(f.get("type")),
        "date": _s(date.get("original")) or _s(date.get("formal")),
        "place": _s(_d(f.get("place")).get("original")),
    }


def _facts(v: Any) -> list[dict]:
    return [_fact(f) for f in _l(v)]


def project_person(person: Any) -> dict:
    person = _d(person)
    display = _d(person.get("display"))
    gender = _s(display.get("gender")) or _short(_d(person.get("gender")).get("type"))
    return {
        "id": _id(person.get("id")),
        "name": _s(display.get("name")),
        "lifespan": _s(display.get("lifespan")),
        "gender": gender,
        "living": person.get("living") if isinstance(person.get("living"), bool) else None,
        "facts": _facts(person.get("facts")),
        "links_summary": sorted(_d(person.get("links")).keys()),
    }


def _confidence(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, (float, str)):
        try:
            return int(float(v))
        except (ValueError, OverflowError):
            return None
    return None


def project_search_hit(entry: Any) -> dict:
    entry = _d(entry)
    gx = _d(_d(entry.get("content")).get("gedcomx"))
    person = _first(gx.get("persons"))
    display = _d(person.get("display"))
    ark = _s(gx.get("id"))
    pid = _id(person.get("id")) or _id(ark.rsplit(":", 1)[-1] if ark else None)
    return {
        "id": pid,
        "confidence": _confidence(entry.get("confidence")),
        "name": _s(display.get("name")),
        "birth": {"date": _s(display.get("birthDate")), "place": _s(display.get("birthPlace"))},
        "death": {"date": _s(display.get("deathDate")), "place": _s(display.get("deathPlace"))},
        "gender": _s(display.get("gender")),
    }


def project_record_hit(entry: Any) -> dict:
    entry = _d(entry)
    gx = _d(_d(entry.get("content")).get("gedcomx"))
    persons = [_d(p) for p in _l(gx.get("persons"))]
    person = next((p for p in persons if p.get("principal") is True), persons[0] if persons else {})
    name = None
    for n in _l(person.get("names")):
        for form in _l(_d(n).get("nameForms")):
            name = _s(_d(form).get("fullText"))
            if name:
                break
        if name:
            break
    return {
        "id": _id(entry.get("id")) or _id(gx.get("id")),
        "confidence": _confidence(entry.get("confidence")),
        "name": name,
        "facts": _facts(person.get("facts")),
    }


def project_source(desc: Any) -> dict:
    desc = _d(desc)
    return {
        "id": _id(desc.get("id")),
        "title": _s(_first(desc.get("titles")).get("value")),
        "citation": _s(_first(desc.get("citations")).get("value")),
        "about": _s(desc.get("about")),
    }


def project_memory(mem: Any) -> dict:
    mem = _d(mem)
    desc = mem.get("description")
    if isinstance(desc, list):
        desc = _first(desc).get("value")
    return {
        "id": _id(mem.get("id")),
        "title": _s(_first(mem.get("titles")).get("value")),
        "description": _s(desc),
        "link": _s(_d(_d(mem.get("links")).get("description")).get("href")),
    }
