"""Read recipes for the Standards reads: places, place types, vocabularies, dates, names.

Standards data is stable reference data: no fixture is created. Each case reads a canary
(tools/verify/world.py, the CANARY block: real beta values, meant to fail loudly if they
ever change) and anchors on a canary name the request does not send: a place's or a
child place's name, a group's title, a type's or a term's label. The ids sent are
module-level constants of that block and never world values (short numbers would match
by containment). Pure computations without a path parameter (date standardization, name
segmentation and composition) are anchor-free: their literal anchors are what the
server must compute (the formal date, the part types, the composed full name), never
what was sent. Recipes are split by confidence, since one failing case keeps its whole
recipe from being recorded. The reads no sound case can prove are in ``DEFERRED``.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..reads import ReadCase, json_path, reads_recipe
from ..world import (
    CANARY_GROUP, CANARY_LOCALE, CANARY_PLACE, CANARY_PLACE_DESCRIPTION, CANARY_PLACE_GROUP,
    CANARY_PLACE_QUERY, CANARY_PLACE_TYPE, CANARY_PLACE_TYPE_GROUP, CANARY_VOCAB_CONCEPT,
    CANARY_VOCAB_LIST, CANARY_VOCAB_TERM,
)

NO_SUCH_STANDARD = "999999999"  # numeric like every standards id, and absent
NO_MATCH = "Qzxqzvqzxjj"        # a name no place or concept carries
GX = "http://gedcomx.org/"
NEEDS = (CANARY_GROUP,)
# The literals the name and date services must compute (not echoes of the request).
DATE_SENT = "26 Nov 1934"
DATE_FORMAL = "+1934-11-26"
NAME_GIVEN, NAME_SURNAME = "Johann Sebastian", "Bach"
NAME_FULL = f"{NAME_GIVEN} {NAME_SURNAME}"
NO_PLACES_EMPTY = (
    "the spec documents 204 for a search with no match ('If no places were found' / 'No "
    "places found that contain the search value'); it counts only because the positive "
    "call in the same recipe answered the canary place's full name, which the request "
    "did not send")
NO_CONCEPTS_EMPTY = (
    "the spec documents 204 when 'No concepts were found that match the given search "
    "criteria'; it counts only because the positive call answered the canary concept")

DEFERRED: dict[str, str] = {
    "readPlaceTypes": (
        "takes no path or query parameter, so no not-found control can be built: every "
        "request reads the same list, and an unknown query parameter is ignored (its 404 "
        "is documented only for 'the list cannot be retrieved'); readPlaceType proves the "
        "type reads"),
    "readPlaceTypeGroups": (
        "takes no path or query parameter, so no not-found control can be built: every "
        "request reads the same list of type groups; readPlaceTypeGroup proves the reads"),
    "checkPlaceIsChild": (
        "answers yes or no about the two place ids the request sends: the answer carries "
        "no string the request did not send (a boolean survives the framework's string "
        "scrub), so no anchor is possible, and the pid path parameter rules out an "
        "anchor-free case; recon never saw a 200"),
    "getNameScript": (
        "recon shows the answer as {\"Katakana\": <string>}: the script is only a dict key "
        "(an anchor in a key is rejected) and the value is most likely the echoed text, "
        "which an anchor may not be, so no sound content check exists"),
}


# ------------------------------------------------------------------ checks


def _strings(value: Any) -> Iterator[str]:
    """Every string value (not key) in a JSON body."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _dicts(value: Any) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _dicts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _dicts(v)


def _places(body: Any) -> list[dict]:
    """The place descriptions of an answer: ``places``, or each search entry's places."""
    found = []
    for d in _dicts(body):
        places = d.get("places")
        if isinstance(places, list):
            found += [p for p in places if isinstance(p, dict)]
    return found


def _place_names(place: dict) -> set[str]:
    names = set()
    display = place.get("display")
    if isinstance(display, dict):
        names |= {v for k, v in display.items() if k in ("name", "fullName")
                  and isinstance(v, str)}
    for name in place.get("names") or []:
        if isinstance(name, dict) and isinstance(name.get("value"), str):
            names.add(name["value"])
    return names


def _place(body: Any, pdid: str) -> dict:
    for place in _places(body):
        if str(place.get("id")) == pdid:
            return place
    raise AssertionError(f"no place {pdid} in the response")


def _expect_place(name_key: str, pdid: str = CANARY_PLACE_DESCRIPTION, *,
                  exactly: bool = False, child_key: str | None = None):
    """The place ``pdid`` under its canary name; optionally the only place, or with a
    canary child place among the others."""

    def expect(result, world):
        place, want = _place(result.body, pdid), world.names[name_key]
        assert want in _place_names(place), (
            f"place {pdid} is named {sorted(_place_names(place))}, not {want!r}")
        if exactly:
            ids = [str(p.get("id")) for p in _places(result.body)]
            assert ids == [pdid], f"places are {ids}, not only the requested {pdid}"
        if child_key:
            _expect_child(child_key)(result, world)

    return expect


def _expect_child(child_key: str):
    def expect(result, world):
        want = world.names[child_key]
        places = _places(result.body)
        assert any(want in _place_names(p) for p in places), f"no child place {want!r}"

    return expect


def _expect_children(result, world):
    """The canary's children: a child place, and not the canary itself."""
    _expect_child("place_child_name")(result, world)
    ids = {str(p.get("id")) for p in _places(result.body)}
    assert CANARY_PLACE_DESCRIPTION not in ids, "the children hold the parent itself"


def _expect_labelled(label_key: str, rid: str | None = None, *, under: str | None = None):
    """A string value equal to the canary ``label_key`` (under ``under`` if given), and
    (with ``rid``) an ``id``/``@id`` naming the requested resource."""

    def expect(result, world):
        body = json_path(result.body, under) if under else result.body
        want = world.names[label_key]
        assert want in set(_strings(body)), f"no value {want!r}" + (
            f" under {under}" if under else "")
        if rid is not None:
            ids = [str(d.get(k)) for d in _dicts(result.body) for k in ("id", "@id")
                   if k in d]
            assert any(i == rid or i.rstrip("/").endswith("/" + rid) for i in ids), (
                f"no id names {rid}")

    return expect


def _expect_full_name(result, world):
    """A search hit carries the canary's standard full name."""
    want = world.names["provo_full_name"]
    assert any(want in _place_names(p) for p in _places(result.body)), (
        f"no place hit named {want!r}")


def _expect_formal_date(result, world):
    text = (result.text or "").strip()
    assert DATE_FORMAL in text, f"the standardized date is {text[:80]!r}, not {DATE_FORMAL}"


def _parts(body: Any) -> list[dict[str, str]]:
    """Each name form's parts as {type: value}."""
    found = []
    for d in _dicts(body):
        parts = d.get("parts")
        if isinstance(parts, list):
            found.append({p.get("type"): p.get("value") for p in parts if isinstance(p, dict)})
    return found


def _expect_segments(result, world):
    forms = _parts(result.body)
    want = {GX + "Given": NAME_GIVEN, GX + "Surname": NAME_SURNAME}
    assert any(all(form.get(k) == v for k, v in want.items()) for form in forms), (
        f"no name form segments {NAME_FULL!r} as {want}: {forms[:3]}")


def _expect_composed(result, world):
    texts = {d.get("fullText") for d in _dicts(result.body)}
    assert NAME_FULL in texts, f"no fullText {NAME_FULL!r} composed from the parts"


# ------------------------------------------------------------------ cases


def _canary(key: str):
    return lambda w: w.names[key]


PDID = lambda w: {"pdid": CANARY_PLACE_DESCRIPTION}  # noqa: E731
MISSING_PDID = {"pdid": NO_SUCH_STANDARD}

PLACE_DESCRIPTIONS_CASES = (
    ReadCase(op="readPlaceDescription", path_args=PDID, missing=MISSING_PDID,
             expect=_expect_place("place_name"), anchors=(_canary("place_name"),),
             needs=NEEDS),
    ReadCase(op="readPlaceDescriptions", path_args=lambda w: {},
             query={"pdids": CANARY_PLACE_DESCRIPTION},
             missing={}, missing_query={"pdids": NO_SUCH_STANDARD},
             expect=_expect_place("place_name", exactly=True),
             anchors=(_canary("place_name"),), needs=NEEDS),
    ReadCase(op="readPlaceDescriptionWithRelated", path_args=PDID, missing=MISSING_PDID,
             expect=_expect_place("place_name", child_key="place_child_name"),
             anchors=(_canary("place_name"), _canary("place_child_name")), needs=NEEDS),
    ReadCase(op="readPlaceChildren", path_args=PDID, missing=MISSING_PDID,
             expect=_expect_children, anchors=(_canary("place_child_name"),), needs=NEEDS),
)
PLACE_DESCRIPTIONS = reads_recipe("reads.standards.place_descriptions",
                                  PLACE_DESCRIPTIONS_CASES)

PLACE_GROUPS_CASES = (
    ReadCase(op="readPlaceDescriptionsGroup", path_args=lambda w: {"pgid": CANARY_PLACE_GROUP},
             missing={"pgid": NO_SUCH_STANDARD},
             expect=_expect_labelled("place_group_title", CANARY_PLACE_GROUP),
             anchors=(_canary("place_group_title"),), needs=NEEDS),
    ReadCase(op="readPlaceAttributes", path_args=PDID, missing=MISSING_PDID,
             expect=_expect_labelled("place_attribute_value"),
             anchors=(_canary("place_attribute_value"),), needs=NEEDS),
)
PLACE_GROUPS = reads_recipe("reads.standards.place_groups", PLACE_GROUPS_CASES)

# The ids here are guesses (the spec gives no example): see the CANARY block.
PLACES_BY_ID_CASES = (
    ReadCase(op="readPlace", path_args=lambda w: {"pid": CANARY_PLACE},
             missing={"pid": NO_SUCH_STANDARD},
             # A place answers its descriptions, whose ids are description ids: the name
             # is what ties the answer to the place asked for.
             expect=_expect_child("place_by_id_name"),
             anchors=(_canary("place_by_id_name"),), needs=NEEDS),
    ReadCase(op="readPlaceType", path_args=lambda w: {"ptid": CANARY_PLACE_TYPE},
             missing={"ptid": NO_SUCH_STANDARD},
             expect=_expect_labelled("place_type_label", CANARY_PLACE_TYPE),
             anchors=(_canary("place_type_label"),), needs=NEEDS),
    ReadCase(op="readPlaceTypeGroup", path_args=lambda w: {"ptgid": CANARY_PLACE_TYPE_GROUP},
             missing={"ptgid": NO_SUCH_STANDARD},
             expect=_expect_labelled("place_type_group_label", CANARY_PLACE_TYPE_GROUP),
             anchors=(_canary("place_type_group_label"),), needs=NEEDS),
)
PLACES_BY_ID = reads_recipe("reads.standards.places_by_id", PLACES_BY_ID_CASES)

# The request sends "Provo"; the server must supply the standard full name.
PLACE_SEARCH_CASES = (
    ReadCase(op="readPlaces", path_args=lambda w: {},
             query={"q": f"name:{CANARY_PLACE_QUERY}"},
             missing={}, missing_query={"q": f"name:{NO_MATCH}"},
             control_may_be_empty=NO_PLACES_EMPTY, expect=_expect_full_name,
             anchors=(_canary("provo_full_name"),), needs=NEEDS),
    ReadCase(op="searchForParentPlaces", path_args=lambda w: {},
             query={"value": CANARY_PLACE_QUERY},
             missing={}, missing_query={"value": NO_MATCH},
             control_may_be_empty=NO_PLACES_EMPTY, expect=_expect_full_name,
             anchors=(_canary("provo_full_name"),), needs=NEEDS),
)
PLACE_SEARCH = reads_recipe("reads.standards.place_search", PLACE_SEARCH_CASES)

VOCAB_CASES = (
    ReadCase(op="readVocabList", path_args=lambda w: {"cvlid": CANARY_VOCAB_LIST},
             missing={"cvlid": NO_SUCH_STANDARD},
             expect=_expect_labelled("vocab_list_title", CANARY_VOCAB_LIST),
             anchors=(_canary("vocab_list_title"),), needs=NEEDS),
    ReadCase(op="readVocabTerm", path_args=lambda w: {"cvtid": CANARY_VOCAB_TERM},
             missing={"cvtid": NO_SUCH_STANDARD},
             expect=_expect_labelled("vocab_term_label", CANARY_VOCAB_TERM),
             anchors=(_canary("vocab_term_label"),), needs=NEEDS),
    ReadCase(op="readVocabConceptV2", path_args=lambda w: {"cvcid": CANARY_VOCAB_CONCEPT},
             missing={"cvcid": NO_SUCH_STANDARD},
             expect=_expect_labelled("vocab_term_label", under="vocabConcepts"),
             anchors=(_canary("vocab_term_label"),), needs=NEEDS),
)
VOCAB = reads_recipe("reads.standards.vocab", VOCAB_CASES)

VOCAB_MORE_CASES = (
    ReadCase(op="readVocabConceptsSearch", path_args=lambda w: {},
             query={"q": 'string:"List"', "start": 0, "count": 20},
             missing={}, missing_query={"q": f'string:"{NO_MATCH}"', "start": 0, "count": 20},
             control_may_be_empty=NO_CONCEPTS_EMPTY,
             expect=_expect_labelled("vocab_search_hit", under="entries"),
             anchors=(_canary("vocab_search_hit"),), needs=NEEDS),
    ReadCase(op="readVocabTermTranslation",
             path_args=lambda w: {"cvtid": CANARY_VOCAB_TERM, "locale": CANARY_LOCALE},
             missing={"cvtid": NO_SUCH_STANDARD},
             expect=_expect_labelled("vocab_term_label"),
             anchors=(_canary("vocab_term_label"),), needs=NEEDS),
    ReadCase(op="readVocabConceptDefinition",
             path_args=lambda w: {"cvcid": CANARY_VOCAB_CONCEPT, "locale": CANARY_LOCALE},
             missing={"cvcid": NO_SUCH_STANDARD},
             expect=_expect_labelled("vocab_definition"),
             anchors=(_canary("vocab_definition"),), needs=NEEDS),
)
VOCAB_MORE = reads_recipe("reads.standards.vocab_more", VOCAB_MORE_CASES)

DATES_NAMES_CASES = (
    ReadCase(op="standardizeDate", path_args=lambda w: {}, query={"date": DATE_SENT},
             missing={}, missing_query={"date": NO_MATCH}, missing_statuses=(400,),
             expect=_expect_formal_date,
             anchor_free_reason=("standards computation, no fixture: the formal date of "
                                 "an informal one"),
             literal_anchors=(DATE_FORMAL,)),
    # Recon: an empty fullName answers 400 ("A name must be provided.").
    ReadCase(op="getNameSegments", path_args=lambda w: {}, query={"fullName": NAME_FULL},
             missing={}, missing_query={"fullName": ""}, missing_statuses=(400,),
             expect=_expect_segments,
             anchor_free_reason=("standards computation, no fixture: a full name split "
                                 "into typed parts"),
             literal_anchors=(GX + "Given", GX + "Surname")),
)
DATES_NAMES = reads_recipe("reads.standards.dates_names", DATES_NAMES_CASES)


def _name_body(parts: list[dict]) -> dict:
    return {"persons": [{"names": [{"nameForms": [{"parts": parts}]}]}]}


COMPOSE_NAME_CASES = (
    # A read POST: the parts are sent apart, the server must join them.
    ReadCase(op="createNameSegments", path_args=lambda w: {},
             body=_name_body([{"type": GX + "Given", "value": NAME_GIVEN},
                              {"type": GX + "Surname", "value": NAME_SURNAME}]),
             missing={}, missing_body=_name_body([]), missing_statuses=(400,),
             expect=_expect_composed,
             anchor_free_reason=("standards computation, no fixture: a full name composed "
                                 "from typed parts"),
             literal_anchors=(NAME_FULL,)),
)
COMPOSE_NAME = reads_recipe("reads.standards.compose_name", COMPOSE_NAME_CASES)

READS_STANDARDS = (PLACE_DESCRIPTIONS, PLACE_GROUPS, PLACES_BY_ID, PLACE_SEARCH, VOCAB,
                   VOCAB_MORE, DATES_NAMES, COMPOSE_NAME)
