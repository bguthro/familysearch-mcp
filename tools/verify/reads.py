"""Data-driven read verification: one ``ReadCase`` per read operation.

A case passes only if all of these hold, in one run against the same server:

- the positive call (fixture ids from the world) answers an accepted success status
  (``accepted_statuses``) in the media type that was asked for (``media_type_problem``);
- every ``anchor`` (an exact world id or name) appears in the response, checked by
  the framework, and none survives in the scrubbed mutant (so it is response content,
  not a dict key);
- its ``expect`` (structure and placement) passes on the real result and fails on
  every mutant of it (``mutants``): a check that still passes with every string in the
  response replaced, or with the response emptied, does not depend on the response;
- the not-found control (the same operation with ``missing`` ids, or for a
  parameterless operation ``missing_query``/``missing_body``) reaches the server and
  answers one of ``missing_statuses``, so a catch-all server cannot pass.

HEAD cases read their content from the matching GET (``compare_with``), sent with that
GET's Accept, and must share an HTTP validator with it; OPTIONS cases must answer a
different ``Allow`` for ``contrast_args``. A content or validator failure carries a
bounded ``skeleton`` of the response, so a live failure shows the real shape.
``reads_recipe`` groups cases into a ``read-content`` recipe and reports every failing
case at once. Fixtures come from a ``World`` (built by
tools/verify/world.py) on the beta client with the hand-written library, never through
the generated API; cases only ever see a sealed, read-only view of it (``seal``).
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import time
import uuid
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import cache
from types import MappingProxyType
from typing import Any, Protocol

from familysearch.api import OPERATIONS
from familysearch.api._runtime import ApiResult
from familysearch.api.spec import OpSpec, is_write
from familysearch.errors import FSHTTPError

from .core import Recipe, VerifyContext

MAX_CASES = 12  # one bad case should fail only a small group
# Not-found means the id (or query) does not exist; nothing else proves the server looked.
NOT_FOUND_STATUSES = frozenset({400, 404, 410})
_VALIDATORS = ("ETag", "Last-Modified", "Content-Length")
_BODY_LIMIT = 200
SKELETON_LIMIT = 1500  # characters of response shape a failure may carry
# Dict levels shown (lists do not count). 6, not 4: a search hit's person sits at
# entries[].content.gedcomx.persons[] (level 4) and its name one level below; the
# total stays bounded by SKELETON_LIMIT.
_SKELETON_DEPTH = 6
_SKELETON_STRING = 60  # characters of each string shown
# Keys whose values are never shown, whatever the response holds.
_SECRET_KEY = re.compile(r"token|authori[sz]ation|cookie|session|secret|password", re.I)
MAX_ATTEMPTS = 10   # ``eventual`` bounds: at most this many positive calls...
MAX_PAUSE = 60.0    # ...this many seconds apart
sleep = time.sleep  # the pause between ``eventual`` attempts (tests replace it)

# Recipes reads_recipe built, by identity: id -> (weak ref, built with default_world).
# Only reads_recipe writes here, so a hand-made or copied Recipe is never in it.
_BUILT: dict[int, tuple[weakref.ref, bool]] = {}
_test_factories = 0  # > 0 inside allow_test_world_factories()


class World(Protocol):
    """What a case sees of the fixture world (tools/verify/world.py, built per recipe).

    ``need`` creates the named fixture groups that do not exist yet; ``ids`` and
    ``names`` hold the created objects' ids and the display strings to look for.
    Anchors are checked by membership in these values, so the world must keep shared
    constants (the ``ZZSPIKE`` prefix, a surname common to every fixture, the signed-in
    user's id) out of them, or give each fixture a distinct value: a value every
    fixture shares would let another fixture's record pass as this one's.
    """

    ids: Mapping[str, str]
    names: Mapping[str, str]

    def need(self, *groups: str) -> None: ...


@dataclasses.dataclass(frozen=True)
class ReadCase:
    """How to read one operation for a fixture, check the content, and check not-found.

    ``anchors`` give the fixture values (each must be exactly one of the world's ids
    or names) the framework requires in the response: the body, or the header values
    for HEAD/OPTIONS without a sibling GET. At least one must not be contained in any
    string the request sends (path args, query values and list items, body strings):
    a server echoing the requested ids proves nothing about the record. Every
    populated case needs anchors, except OPTIONS (its contrast stands in) and a case
    that declares ``anchor_free_reason``.

    ``anchor_free_reason`` is allowed only for an operation without path params and a
    case without ``needs``, and needs ``literal_anchors``. For such a case the
    framework enforces exactly this: the request sends no string containing any world
    id or name; each literal anchor (a spec constant, such as a place-type URI) is in
    the response, not only in a dict key, not in what the request sent and not in the
    not-found control's error body; and ``expect`` fails on the mutants. Whether the
    response is the right one beyond that (e.g. ``expect`` comparing against a value
    discovered earlier in the recipe) is up to the case author and is not checked.
    ``literal_anchors`` may also be given to a fixture-anchored case, with the same
    checks.

    ``expect(result, world)`` checks structure and placement; it raises
    ``AssertionError`` on a mismatch and returns None. ``missing`` overrides path args
    with ids that must not exist (``{}`` for a parameterless operation, whose control
    is ``missing_query`` and/or ``missing_body`` instead). ``allow_empty`` accepts a
    204 for this case; it needs a populated case of the same operation, with other
    args or query, that passes first in the same recipe (a 204 is then a real
    "empty", not a server that answers 204 to everything).

    ``compare_with`` (HEAD only, required) names the GET
    with the same path: it is called with the same args, the anchors and ``expect``
    run on its result, and HEAD and GET must agree on an ETag, Last-Modified or
    Content-Length. ``contrast_args`` (OPTIONS only, required) are other path args
    whose ``Allow`` must differ from the positive call's. A HEAD case is sent with its
    GET's Accept (HEAD documents none; the server answers 406 to another type).

    ``control_may_be_empty`` (a non-empty reason) lets the not-found control answer an
    empty 204 instead of 400/404/410. Only for an operation whose spec documents 204,
    and a case with fixture anchors that is not ``allow_empty``: its positive call
    answered anchored content (a 204 there fails), so the empty answer for the unknown
    id is a real "nothing", not a server that answers 204 to everything.

    ``eventual`` = ``(max_attempts, seconds_between)`` repeats the positive call (never
    the control) while it answers 204, no content, or lacks an anchor, for an index
    that lags behind fixtures created a moment ago (search). Bounded by ``MAX_ATTEMPTS``
    and ``MAX_PAUSE``; the last answer is checked as usual, so it still has to pass.
    """

    op: str
    path_args: Callable[[World], dict[str, str]]
    expect: Callable[[ApiResult, World], None]
    missing: dict[str, str]
    query: Callable[[World], dict] | dict | None = None
    missing_query: dict | None = None
    accept: str | None = None
    body: Any = None
    missing_statuses: tuple[int, ...] = (400, 404, 410)
    needs: tuple[str, ...] = ()
    allow_empty: bool = False
    missing_body: Any = None
    anchors: tuple[Callable[[World], str], ...] = ()
    anchor_free_reason: str | None = None
    compare_with: str | None = None
    contrast_args: Callable[[World], dict] | None = None
    literal_anchors: tuple[str, ...] = ()
    control_may_be_empty: str | None = None
    eventual: tuple[int, float] | None = None


# ------------------------------------------------------------------ the world


class _SealedWorld:
    """A frozen copy of a world's ``ids`` and ``names``: nothing else, and no ``need``."""

    __slots__ = ("ids", "names")

    def __init__(self, ids: Mapping[str, str], names: Mapping[str, str]):
        object.__setattr__(self, "ids", MappingProxyType(dict(ids)))
        object.__setattr__(self, "names", MappingProxyType(dict(names)))

    def __setattr__(self, name, value):
        raise AttributeError("the world is sealed: cases cannot change it")

    def need(self, *groups: str) -> None:
        raise AssertionError("the world is sealed: fixtures are built up front from the "
                             "cases' needs, not from inside a case")


def seal(world: World) -> World:
    """The read-only view handed to ``path_args``, ``query``, ``anchors`` and ``expect``.

    No client, fixtures or ``need``: a case can read the fixtures, never make more.
    """
    if isinstance(world, _SealedWorld):
        return world
    return _SealedWorld(world.ids, world.names)


# ------------------------------------------------------------------ the spec


@cache
def _snapshot_responses() -> dict[str, dict]:
    """Generated operation id -> the documented ``responses`` of its source operation.

    Resolved through the overrides, so a renamed operation finds its source entry.
    """
    from tools.spec.resolve import SNAPSHOT_PATH, load_overrides, resolve

    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    by_source = {o["operation_id"]: o.get("responses") or {} for o in snapshot["operations"]}
    ops, _excluded = resolve(snapshot, load_overrides())
    return {o.operation_id: by_source.get(o.source_operation_id, {}) for o in ops}


def documented_statuses(op: str) -> frozenset[int]:
    """The 2xx and 3xx statuses the spec documents for ``op``."""
    codes = _snapshot_responses().get(op, {})
    return frozenset(int(c) for c in codes if c.isdigit() and 200 <= int(c) < 400)


def accepted_statuses(op: str, *, allow_empty: bool = False) -> frozenset[int]:
    """The final statuses a positive call may answer.

    The documented 2xx except 204; plus 200 when a redirect is documented (the client
    follows 301/303/307, so ``readCurrentTreePerson``, documented only as 303, ends
    in a 200); plus 204 only for an ``allow_empty`` case. The spec documents 204
    unevenly (some list reads answer an undocumented 204 when empty, some document
    it), so 204 is decided by the case, never by the spec. An operation that
    documents only 204 (``readPartnerEligibility``) therefore accepts no 200.
    """
    documented = documented_statuses(op)
    accepted = {s for s in documented if 200 <= s < 300 and s != 204}
    if any(300 <= s < 400 for s in documented):
        accepted.add(200)
    if allow_empty:
        accepted.add(204)
    return frozenset(accepted)


def _bare_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _family(media_type: str) -> str:
    """``json`` for application/json and any ``+json``, ``xml`` likewise, else itself."""
    subtype = media_type.rsplit("/", 1)[-1]
    for family in ("json", "xml"):
        if subtype == family or subtype.endswith("+" + family):
            return family
    return media_type


def media_type_problem(spec: OpSpec, requested: str | None, content_type: str) -> str | None:
    """Why ``content_type`` does not answer ``requested`` for ``spec``, or None.

    An exact match (parameters ignored) passes. So does another type from the
    operation's own ``accepts`` list in the same family (the server may answer
    ``application/x-gedcomx-v1+json`` to ``application/x-fs-v1+json``, or
    ``application/json``, when the operation lists both). A type the operation
    does not list, or one from another family (xml for json), fails. An operation
    with no ``accepts`` (HEAD, OPTIONS, redirecting GETs) is not checked.
    """
    if not spec.accepts or requested is None:
        return None
    got, want = _bare_type(content_type), _bare_type(requested)
    if got == want:
        return None
    listed = {_bare_type(a) for a in spec.accepts}
    if got in listed and _family(got) == _family(want):
        return None
    return f"media type {got or '(none)'!r} does not answer Accept {want!r}"


# ------------------------------------------------------------------ helpers


def _serialized(result: ApiResult) -> str:
    if result.body is not None:
        return json.dumps(result.body, ensure_ascii=False)
    return result.text or ""


def _header_text(result: ApiResult) -> str:
    return "\n".join(str(v) for v in result.headers.values())


def contains(result: ApiResult, *needles: str) -> None:
    """Each needle appears in the JSON-serialized body (the text if not JSON)."""
    if not needles:
        raise ValueError("contains needs at least one needle")
    haystack = _serialized(result)
    for needle in needles:
        if not (isinstance(needle, str) and needle):
            raise ValueError(f"needle must be a non-empty string, got {needle!r}")
        if needle not in haystack:
            raise AssertionError(f"{needle!r} not in the response body")


_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def json_path(body: Any, path: str) -> Any:
    """``body`` at ``"a.b[0].c"``, or AssertionError naming the first step that is absent."""
    value, walked = body, ""
    for m in _TOKEN.finditer(path):
        key, index = m.group(1), m.group(2)
        if key is not None:
            walked += ("." if walked else "") + key
            if not isinstance(value, dict) or key not in value:
                raise AssertionError(f"no {walked} in the response body")
            value = value[key]
        else:
            walked += f"[{index}]"
            if not isinstance(value, list) or int(index) >= len(value):
                raise AssertionError(f"no {walked} in the response body")
            value = value[int(index)]
    return value


def _scrub(value: Any, marker: str) -> Any:
    """Every string value replaced by ``marker``; dict keys and numbers are kept, so a
    check that finds a fixture value only in a key (or only counts) still passes."""
    if isinstance(value, str):
        return marker
    if isinstance(value, dict):
        return {k: _scrub(v, marker) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, marker) for v in value]
    return value


def mutants(result: ApiResult) -> list[ApiResult]:
    """Copies of ``result`` a content check must reject.

    One with every string value replaced by a fresh random marker (body, text and
    header values; keys and numbers kept), one with no body, text or headers. A check
    tied to a fixture's id or name fails on both; one that looks only at status, keys
    or counts does not. The marker is a fresh ``uuid4().hex`` (32 lowercase hex
    digits, no fixed part): a check cannot expect a constant, though it could still
    recognise the shape. An anchor with an uppercase letter, a space or punctuation
    cannot occur in it; an all-lowercase-hex or all-digit anchor could, by chance.
    """
    marker = uuid.uuid4().hex
    body = _scrub(result.body, marker)
    text = json.dumps(body) if result.body is not None else (marker if result.text else "")
    scrubbed = dataclasses.replace(
        result, body=body, text=text, headers={k: marker for k in result.headers})
    emptied = dataclasses.replace(result, body=None, text="", headers={})
    return [scrubbed, emptied]


def _shape(value: Any, depth: int) -> Any:
    if isinstance(value, dict):
        if depth >= _SKELETON_DEPTH:
            return "{...}"
        return {k: "<hidden>" if _SECRET_KEY.search(str(k)) else _shape(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, list):
        return [f"list[{len(value)}]", _shape(value[0], depth)] if value else []
    if isinstance(value, str):
        cut = value[:_SKELETON_STRING]
        return cut + "..." if len(value) > _SKELETON_STRING else cut
    return value


def skeleton(result: ApiResult, *, headers: bool = False) -> str:
    """A bounded picture of a response, for a failure message: never a secret.

    The status and content type, then the JSON body's keys and values (strings cut
    to 60 characters, a list as its length and first item, dict levels below 6
    elided, values of token/cookie/authorization-like keys hidden) or the text's
    start. With ``headers`` (HEAD/OPTIONS), the header names and only the validator
    and ``Allow`` values. Only the response is shown: the request (and its token)
    never reaches an ``ApiResult``. At most ``SKELETON_LIMIT`` characters.
    """
    parts = [f"{result.status} {result.content_type or '(no type)'}"]
    if headers:
        shown = {k: v for k, v in result.headers.items()
                 if k.lower() in {n.lower() for n in (*_VALIDATORS, "Allow")}}
        parts.append(f"headers {sorted(result.headers)} {json.dumps(shown)}")
    if result.body is not None:
        parts.append(json.dumps(_shape(result.body, 0), ensure_ascii=False))
    elif (result.text or "").strip():
        parts.append(f"text[{len(result.text)}] {_shape(result.text, 0)!r}")
    elif not headers:
        parts.append("(empty)")
    text = " ".join(parts)
    return text if len(text) <= SKELETON_LIMIT else text[:SKELETON_LIMIT - 3] + "..."


class ContentFailure(AssertionError):
    """A content or validator failure, with the response skeleton(s) it was judged on."""

    def __init__(self, message: str, shape: str):
        super().__init__(f"{message} | response: {shape}")
        self.message, self.shape = message, shape


def _failure_text(e: Exception) -> str:
    """The failure message for a recipe report: the skeleton is kept whole (bounded)."""
    if isinstance(e, ContentFailure):
        return f"{_snippet(AssertionError(e.message))} | response: {e.shape}"
    return _snippet(e)


def _check_content(case: ReadCase, result: ApiResult, world: World, anchors: list[str],
                   in_headers: bool) -> None:
    try:
        _check_content_of(case, result, world, anchors, in_headers)
    except ContentFailure:
        raise
    except AssertionError as e:
        raise ContentFailure(str(e), skeleton(result, headers=in_headers)) from None


def _check_content_of(case: ReadCase, result: ApiResult, world: World, anchors: list[str],
                      in_headers: bool) -> None:
    """Anchors in the response (and not in its scrubbed copy), then ``expect`` on it and,
    failing, on every mutant."""
    haystack = _header_text if in_headers else _serialized
    where = "response headers" if in_headers else "response"
    for anchor in anchors:
        if anchor not in haystack(result):
            raise AssertionError(f"anchor {anchor!r} not in the {where}")
    returned = case.expect(result, world)
    if returned is not None:
        raise AssertionError(
            f"expect returned {returned!r} instead of raising: use assert, contains or "
            "json_path so a mismatch fails")
    for mutant in mutants(result):
        for anchor in anchors:
            if anchor in haystack(mutant):
                raise AssertionError(f"anchor {anchor!r} survives with every string value "
                                     "replaced (it is only in a key): not response content")
        try:
            case.expect(mutant, world)
        except Exception:  # noqa: BLE001 - any failure means the check saw the change
            continue
        raise AssertionError(
            "expect does not depend on the response: it still passes with the response's "
            "strings replaced or the response emptied; check a fixture's id or name")


def _anchor_values(case: ReadCase, world: World) -> list[str]:
    known = set(world.ids.values()) | set(world.names.values())
    values = []
    for anchor in case.anchors:
        value = anchor(world)
        if not isinstance(value, str) or value not in known:
            raise AssertionError(f"anchor {value!r} is not a world id or name")
        values.append(value)
    return values


def _strings(value: Any) -> Iterator[str]:
    """Every string leaf of a query, path args or JSON body (values only, as sent)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


def _sent_problem(case: ReadCase, world: World, anchors: list[str], args: dict, query,
                  body) -> str | None:
    """What the request sends must not be what the check looks for (by containment).

    An anchor counts as an echo if it is a substring of any sent string (``pids`` of
    ``"A,B"`` echoes both A and B); at least one anchor must not be. No literal anchor
    may be sent. An anchor-free case may send no world id or name at all: it has no
    fixture anchor, so a fixture value in its request would go unchecked.
    """
    sent = [*_strings(args), *_strings(query), *_strings(body)]

    def is_sent(value: str) -> bool:
        return any(value in s for s in sent)

    if case.anchor_free_reason is not None:
        fixture = sorted({v for v in (*world.ids.values(), *world.names.values())
                          if isinstance(v, str) and v and is_sent(v)})
        if fixture:
            return (f"anchor-free case sends fixture value(s) {', '.join(map(repr, fixture))}; "
                    "a case that reads a fixture must anchor on it")
    if anchors and all(is_sent(a) for a in anchors):
        return (f"every anchor ({', '.join(map(repr, anchors))}) echoes a value the request "
                "sent; add one the response must supply (a name, or a related id)")
    echoed = [lit for lit in case.literal_anchors if is_sent(lit)]
    if echoed:
        return (f"literal anchor(s) {', '.join(map(repr, echoed))} echo a value the request "
                "sent")
    return None


# ------------------------------------------------------------------ cases


def _resolve(value, world: World):
    return value(world) if callable(value) else value


def _snippet(e: Exception) -> str:
    text = str(e)
    return text if len(text) <= _BODY_LIMIT else text[:_BODY_LIMIT] + "..."


def _call(ctx: VerifyContext, spec: OpSpec, args: dict, query, accept, body) -> ApiResult:
    kwargs: dict[str, Any] = {"accept": accept, "query": query}
    if body is not None:  # GET methods take no body argument at all
        kwargs["body"] = body
    return getattr(ctx.api, spec.method_name)(**args, **kwargs)


def _header(result: ApiResult, name: str) -> str | None:
    for key, value in result.headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _validator(value: str) -> str:
    """An ETag without its weak ``W/`` prefix, quotes or a ``-gzip`` suffix."""
    value = value.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip('"')
    return value[:-5] if value.endswith("-gzip") else value


def _sibling_get(ctx: VerifyContext, case: ReadCase, head: ApiResult, args: dict,
                 query) -> ApiResult:
    """The matching GET (same path args and Accept), which must answer and share a
    validator."""
    spec = OPERATIONS[case.compare_with]
    accept = _sibling_accept(case)
    try:
        got = _call(ctx, spec, args, query, accept, None)
    except FSHTTPError as e:
        raise AssertionError(f"sibling {case.compare_with} failed: {_snippet(e)}") from None
    if not 200 <= got.status < 300 or got.status == 204:
        raise AssertionError(f"sibling {case.compare_with} answered {got.status}, not content")
    problem = media_type_problem(spec, accept, got.content_type)
    if problem:
        raise AssertionError(f"sibling {case.compare_with}: {problem}")
    for name in _VALIDATORS:
        h, g = _header(head, name), _header(got, name)
        if h and g and _validator(h) == _validator(g):
            return got
    raise ContentFailure(
        f"HEAD and {case.compare_with} share no equal validator ({', '.join(_VALIDATORS)})",
        f"HEAD {skeleton(head, headers=True)} | GET {skeleton(got, headers=True)}")


def _sibling_accept(case: ReadCase) -> str | None:
    """The Accept a HEAD case and its GET both send: the GET's default."""
    spec = OPERATIONS[case.compare_with]
    return spec.accepts[0] if spec.accepts else None


def _allow(result: ApiResult) -> frozenset[str]:
    return frozenset(m.strip().upper() for m in (_header(result, "Allow") or "").split(",")
                     if m.strip())


def _contrast(ctx: VerifyContext, spec: OpSpec, case: ReadCase, world: World, args: dict,
              query, accept, positive: ApiResult) -> None:
    """OPTIONS for other args must answer another ``Allow``: the answer depends on the ids."""
    other = dict(case.contrast_args(world) or {})
    if other == args:
        raise AssertionError("contrast_args are the positive call's own args")
    allow = _allow(positive)
    if not allow:
        raise AssertionError("OPTIONS answered no Allow header")
    try:
        answer = _call(ctx, spec, other, query, accept, None)
    except FSHTTPError as e:
        raise AssertionError(f"OPTIONS contrast call failed: {_snippet(e)}") from None
    if not _allow(answer):
        raise AssertionError("OPTIONS contrast call answered no Allow header")
    if _allow(answer) == allow:
        raise AssertionError(f"OPTIONS answers the same Allow ({', '.join(sorted(allow))}) "
                             "for the contrast args: it does not depend on the ids")


def run_case(ctx: VerifyContext, world: World, case: ReadCase, *,
             populated: Mapping[str, tuple[dict, Any]] | None = None) -> None:
    """Run one case's positive call, content check and not-found control.

    Raises ValueError for a case ``reads_recipe`` would reject, AssertionError when it
    fails. ``populated`` maps each operation whose populated case already passed to
    that case's (args, query); an ``allow_empty`` case must read other args or query,
    and its 204 counts only if its operation is in it (standalone, nothing vouches for
    it: fail closed). On a 204 there is no content: anchors and ``expect`` are not
    run, and the control still must answer not-found. ``world`` is sealed first.
    """
    problems = _case_problems(case)
    if problems:
        raise ValueError("; ".join(problems))
    _run_case(ctx, seal(world), case, populated or {})


def _run_case(ctx: VerifyContext, world: World, case: ReadCase,
              populated: Mapping[str, tuple[dict, Any]]) -> tuple[dict, Any]:
    spec = OPERATIONS[case.op]
    args = dict(_resolve(case.path_args, world) or {})
    query = _resolve(case.query, world)
    for key, value in case.missing.items():
        if args.get(key) == value:
            raise AssertionError(f"missing {key}={value!r} is the fixture's own value")
    if case.allow_empty and populated.get(case.op) == (args, query):
        raise AssertionError("allow_empty variant reads the same args and query as the "
                             f"populated {case.op} case")
    anchors = _anchor_values(case, world)
    echo = _sent_problem(case, world, anchors, args, query, case.body)
    if echo:
        raise AssertionError(echo)
    anchors += list(case.literal_anchors)  # checked in the response just like anchors
    accept = case.accept or (spec.accepts[0] if spec.accepts else None)
    if spec.http_method == "HEAD" and case.compare_with and accept is None:
        accept = _sibling_accept(case)  # HEAD documents no Accept; its GET's applies
    result = _positive(ctx, spec, case, args, query, accept, anchors)
    allowed = accepted_statuses(case.op, allow_empty=case.allow_empty)
    if result.status not in allowed:
        raise AssertionError(
            f"positive call answered {result.status}, not one of {sorted(allowed)}")
    if result.status == 204:
        if case.op not in populated:
            raise AssertionError(
                f"204 (empty) accepted only after a populated {case.op} case passed")
    elif spec.http_method == "HEAD" and case.compare_with:
        _check_content(case, _sibling_get(ctx, case, result, args, query), world, anchors,
                       in_headers=False)
    elif spec.http_method in ("HEAD", "OPTIONS"):
        _check_content(case, result, world, anchors, in_headers=True)
        if spec.http_method == "OPTIONS":
            _contrast(ctx, spec, case, world, args, query, accept, result)
    else:
        problem = media_type_problem(spec, accept, result.content_type)
        if problem:
            raise AssertionError(problem)
        _check_content(case, result, world, anchors, in_headers=False)
    _control(ctx, spec, case, args, query, accept)
    return args, query


def _pending(result: ApiResult, anchors: list[str]) -> bool:
    """Not there yet (for ``eventual``): empty, or missing an anchor."""
    if result.status == 204 or not (result.body is not None or (result.text or "").strip()):
        return True
    haystack = _serialized(result)
    return any(anchor not in haystack for anchor in anchors)


def _positive(ctx: VerifyContext, spec: OpSpec, case: ReadCase, args: dict, query, accept,
              anchors: list[str]) -> ApiResult:
    """The positive call; with ``eventual``, repeated (bounded) while it is pending.

    An error is never retried: only an empty or anchor-less answer may be index lag.
    """
    attempts, pause = case.eventual or (1, 0.0)
    for attempt in range(attempts):
        try:
            result = _call(ctx, spec, args, query, accept, case.body)
        except FSHTTPError as e:
            raise AssertionError(f"positive call failed: {_snippet(e)}") from None
        if attempt + 1 == attempts or not _pending(result, anchors):
            return result
        sleep(pause)
    raise AssertionError("unreachable")  # pragma: no cover - attempts >= 1


def _control(ctx: VerifyContext, spec: OpSpec, case: ReadCase, args: dict, query,
             accept) -> None:
    """The same operation with ids (or a query) that must not exist: must be not-found.

    Only a server answer counts: a control the client refuses before sending (a
    blank id, a missing required query parameter) proves nothing, so a "missing
    parameter" control must send the parameter with a value that matches nothing.
    """
    bad_args = {**args, **case.missing}
    bad_query = case.missing_query if case.missing_query is not None else query
    bad_body = case.missing_body if case.missing_body is not None else case.body
    if (bad_args, bad_query, bad_body) == (args, query, case.body):
        raise AssertionError("not-found control is identical to the positive call")
    try:
        answer = _call(ctx, spec, bad_args, bad_query, accept, bad_body)
    except FSHTTPError as e:
        if e.status in case.missing_statuses:
            # A literal anchor in the error answer means the server serves the
            # vocabulary whatever it is asked: the positive proves nothing.
            leaked = [lit for lit in case.literal_anchors if lit in (e.body or "")]
            if leaked:
                raise AssertionError(f"not-found control answered {e.status} but carries "
                                     f"literal anchor(s) {', '.join(map(repr, leaked))}")
            return
        raise AssertionError(f"not-found control answered {e.status}, not one of "
                             f"{list(case.missing_statuses)}: {_snippet(e)}") from None
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"not-found control did not reach the server: "
                             f"{type(e).__name__}: {_snippet(e)}") from None
    if (case.control_may_be_empty and answer.status == 204
            and answer.body is None and not (answer.text or "").strip()):
        # Allowed only where the spec documents 204 and the positive call already
        # answered anchored content (construction and the positive check enforce it).
        return
    raise AssertionError(f"not-found control answered {answer.status} for {bad_args or ''}"
                         f"{bad_query or ''}; a server that answers everything verifies nothing")


# ------------------------------------------------------------------ recipes


def _case_problems(case: Any) -> list[str]:
    if not isinstance(case, ReadCase):
        return [f"{case!r} is not a ReadCase"]
    spec = OPERATIONS.get(case.op)
    if spec is None:
        return [f"{case.op}: unknown operation"]
    op, problems = case.op, []
    if spec.kind != "read" or is_write(spec):
        problems.append(f"{op}: not a read operation (kind={spec.kind})")
    if not callable(case.expect):
        problems.append(f"{op}: expect (a content check) is required and must be callable")
    if not callable(case.path_args):
        problems.append(f"{op}: path_args must be a callable taking the world")
    if case.accept is not None and case.accept not in spec.accepts:
        problems.append(f"{op}: accept {case.accept!r} not one of {list(spec.accepts)}")
    if not isinstance(case.missing, dict):
        problems.append(f"{op}: missing must be a dict")
    else:
        extra = sorted(set(case.missing) - set(spec.path_params))
        if extra:
            problems.append(f"{op}: missing keys {extra} are not path parameters")
        if spec.path_params and not case.missing:
            problems.append(f"{op}: missing must override at least one of "
                            f"{list(spec.path_params)} for the not-found control")
    if not spec.path_params and case.missing_query is None and case.missing_body is None:
        problems.append(f"{op}: a parameterless operation needs missing_query "
                        "(or missing_body) for the not-found control")
    if case.missing_query is not None:
        absent = [q for q in spec.required_query if q not in case.missing_query]
        if absent:
            problems.append(f"{op}: missing_query lacks required {absent}; the client "
                            "would refuse it before the server sees it")
    if spec.body == "none" and (case.body is not None or case.missing_body is not None):
        problems.append(f"{op}: takes no body")
    statuses = case.missing_statuses
    if not (isinstance(statuses, tuple) and statuses and set(statuses) <= NOT_FOUND_STATUSES):
        problems.append(f"{op}: missing_statuses must be a non-empty subset of "
                        f"{sorted(NOT_FOUND_STATUSES)}, got {statuses!r}")
    if not all(isinstance(g, str) and g.strip() for g in case.needs):
        problems.append(f"{op}: needs must be non-empty group names")
    problems += _content_problems(case, spec)
    problems += _control_and_polling_problems(case, spec)
    return problems


def _control_and_polling_problems(case: ReadCase, spec: OpSpec) -> list[str]:
    op, problems = case.op, []
    empty = case.control_may_be_empty
    if empty is not None:
        if not (isinstance(empty, str) and empty.strip()):
            problems.append(f"{op}: control_may_be_empty must be a non-empty reason")
        if 204 not in documented_statuses(op):
            problems.append(f"{op}: control_may_be_empty needs an operation whose spec "
                            "documents 204")
        if case.allow_empty or not case.anchors or case.anchor_free_reason is not None:
            problems.append(f"{op}: control_may_be_empty is only for a populated case "
                            "with fixture anchors (not allow_empty or anchor-free)")
    eventual = case.eventual
    if eventual is not None:
        ok = (isinstance(eventual, tuple) and len(eventual) == 2
              and type(eventual[0]) is int and 1 <= eventual[0] <= MAX_ATTEMPTS
              and isinstance(eventual[1], (int, float)) and not isinstance(eventual[1], bool)
              and 0 <= eventual[1] <= MAX_PAUSE)
        if not ok:
            problems.append(f"{op}: eventual must be (attempts 1..{MAX_ATTEMPTS}, seconds "
                            f"0..{MAX_PAUSE:g}), got {eventual!r}")
        if case.allow_empty or spec.http_method in ("HEAD", "OPTIONS"):
            problems.append(f"{op}: eventual is for a populated GET/POST case (not "
                            "allow_empty, HEAD or OPTIONS)")
    return problems


def _content_problems(case: ReadCase, spec: OpSpec) -> list[str]:
    """Anchors, and the HEAD/OPTIONS extras that stand in for a body."""
    op, problems = case.op, []
    if not (isinstance(case.anchors, tuple) and all(callable(a) for a in case.anchors)):
        problems.append(f"{op}: anchors must be a tuple of callables taking the world")
    reason = case.anchor_free_reason
    if reason is not None:
        if not (isinstance(reason, str) and reason.strip()):
            problems.append(f"{op}: anchor_free_reason must be a non-empty explanation")
        if spec.path_params or case.needs:
            # A fixture-bearing case always has a fixture value to anchor on.
            problems.append(f"{op}: anchor_free_reason is only for an operation without "
                            "path params and a case without needs")
        if not case.literal_anchors:
            problems.append(f"{op}: an anchor-free case needs literal_anchors (spec "
                            "constants the response must carry)")
    literals = case.literal_anchors
    if not (isinstance(literals, tuple)
            and all(isinstance(lit, str) and lit.strip() for lit in literals)):
        problems.append(f"{op}: literal_anchors must be a tuple of non-empty strings")
    options =spec.http_method == "OPTIONS"  # Allow carries no fixture: the contrast does
    if not case.anchors and not case.allow_empty and reason is None and not options:
        problems.append(f"{op}: anchors (world ids/names the response must contain) are "
                        "required, or an anchor_free_reason")
    if spec.http_method == "HEAD":
        sibling = OPERATIONS.get(case.compare_with) if case.compare_with else None
        if case.compare_with is None:
            problems.append(f"{op}: a HEAD case needs compare_with (its GET)")
        elif case.compare_with is not None and not (
                sibling and sibling.http_method == "GET" and sibling.kind == "read"
                and sibling.path == spec.path):
            problems.append(f"{op}: compare_with {case.compare_with!r} is not the GET "
                            f"of {spec.path}")
    elif case.compare_with is not None:
        problems.append(f"{op}: compare_with is for HEAD cases only")
    if spec.http_method == "OPTIONS":
        if not callable(case.contrast_args):
            problems.append(f"{op}: an OPTIONS case needs contrast_args (other ids whose "
                            "Allow differs)")
    elif case.contrast_args is not None:
        problems.append(f"{op}: contrast_args is for OPTIONS cases only")
    return problems


def _recipe_problems(cases: Sequence[ReadCase]) -> list[str]:
    if not cases:
        return ["no cases"]
    problems = [p for case in cases for p in _case_problems(case)]
    if len(cases) > MAX_CASES:
        problems.append(f"{len(cases)} cases; a recipe takes at most {MAX_CASES}")
    if problems:
        return problems
    by_op: dict[str, list[ReadCase]] = {}
    for case in cases:
        by_op.setdefault(case.op, []).append(case)
    for op, group in by_op.items():
        populated = [c for c in group if not c.allow_empty]
        if len(populated) > 1:
            problems.append(f"{op}: appears more than once (only allow_empty cases may "
                            "repeat an operation)")
        if not populated:
            problems.append(f"{op}: allow_empty case needs a populated case of the same "
                            "operation in the recipe")
    return problems


def default_world(ctx: VerifyContext) -> World:
    """The fixture world (tools/verify/world.py) on this recipe's client and fixtures."""
    from .world import World as FixtureWorld  # imported late: built by a later task

    return FixtureWorld(ctx.client, ctx.fixtures, ctx.reason, ctx.unique)


@contextlib.contextmanager
def allow_test_world_factories() -> Iterator[None]:
    """Unit tests only: admit recipes built with a fake ``world_factory``.

    Outside this block ``read_recipe_problem`` refuses them, so the runner only ever
    runs the real fixture world (a factory receives the raw client and could write).
    """
    global _test_factories
    _test_factories += 1
    try:
        yield
    finally:
        _test_factories -= 1


def read_recipe_problem(recipe: Recipe) -> str | None:
    """Why ``recipe`` may not run as a read recipe, or None.

    It must be the very object ``reads_recipe`` returned (a hand-made Recipe, or a
    ``dataclasses.replace`` copy with another ``run``, is not), built with
    ``default_world`` unless inside ``allow_test_world_factories``.
    """
    entry = _BUILT.get(id(recipe))
    if entry is None or entry[0]() is not recipe:
        return "read-content recipes must be built with tools.verify.reads.reads_recipe"
    if not entry[1] and not _test_factories:
        return ("read recipe built with a custom world factory; only default_world runs "
                "outside unit tests")
    return None


def reads_recipe(name: str, cases: Sequence[ReadCase], *,
                 world_factory: Callable[[VerifyContext], World] = default_world) -> Recipe:
    """A ``read-content`` recipe proving each case's operation.

    Raises ValueError at construction for any case ``run_case`` would refuse, an
    operation repeated other than as ``allow_empty`` variants, or too many cases.
    ``run`` builds the world once with the union of the cases' ``needs``, then seals
    it: cases see only its ``ids`` and ``names`` and cannot build more. Populated
    cases run before ``allow_empty`` ones; one AssertionError lists every failing
    case. The world's fixtures live in ``ctx.fixtures``, torn down by the runner.
    A HEAD case's sibling GET is called but not declared, so it is not verified here.
    The recipe is registered by identity (``read_recipe_problem``); ``world_factory``
    other than ``default_world`` is for unit tests (``allow_test_world_factories``).
    """
    cases = tuple(cases)
    problems = _recipe_problems(cases)
    if problems:
        raise ValueError(f"bad read recipe {name!r}: " + "; ".join(problems))
    ops = tuple(dict.fromkeys(c.op for c in cases))
    needs = tuple(dict.fromkeys(g for c in cases for g in c.needs))
    ordered = sorted(cases, key=lambda c: c.allow_empty)  # stable: populated first

    def run(ctx: VerifyContext) -> None:
        world = world_factory(ctx)
        if needs:
            world.need(*needs)
        view = seal(world)
        populated: dict[str, tuple[dict, Any]] = {}
        failures = []
        for case in ordered:
            try:
                done = _run_case(ctx, view, case, MappingProxyType(dict(populated)))
            except Exception as e:  # noqa: BLE001 - collect every failing case
                kind = "" if isinstance(e, AssertionError) else f"{type(e).__name__}: "
                failures.append(f"{case.op}: {kind}{_failure_text(e)}")
                continue
            if not case.allow_empty:
                populated[case.op] = done
        if failures:
            raise AssertionError(f"{len(failures)} of {len(cases)} read cases failed:\n  "
                                 + "\n  ".join(failures))

    recipe = Recipe(name, ops, "read-content", run)
    key = id(recipe)
    _BUILT[key] = (weakref.ref(recipe, lambda _ref: _BUILT.pop(key, None)),
                   world_factory is default_world)
    return recipe
