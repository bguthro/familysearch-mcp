"""Data-driven read verification: one ``ReadCase`` per read operation.

A case passes only if all of these hold, in one run against the same server:

- the positive call (fixture ids from the world) answers an accepted success status
  (``accepted_statuses``) in the media type that was asked for (``media_type_problem``);
- its ``expect`` passes on the real result and fails on every mutant of it
  (``mutants``): a check that still passes with every string in the response replaced,
  or with the response emptied, does not depend on the response and proves nothing;
- the not-found control (the same operation with ``missing`` ids, or for a
  parameterless operation ``missing_query``/``missing_body``) reaches the server and
  answers one of ``missing_statuses``, so a catch-all server cannot pass.

``reads_recipe`` groups cases into a ``read-content`` recipe and reports every failing
case at once. Fixtures come from a ``World`` (built by tools/verify/world.py), created
on the beta client with the hand-written library, never through the generated API.
"""
from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from typing import Any, Protocol

from familysearch.api import OPERATIONS
from familysearch.api._runtime import ApiResult
from familysearch.api.spec import OpSpec, is_write
from familysearch.errors import FSHTTPError

from .core import Recipe, VerifyContext

MAX_CASES = 12  # one bad case should fail only a small group
MUTATED = "ZZMUTATED"
# Statuses that say nothing about whether the id exists: auth, throttling, timeouts.
_UNINFORMATIVE = frozenset({401, 403, 407, 408, 429})
_BODY_LIMIT = 200


class World(Protocol):
    """What a case sees of the fixture world (tools/verify/world.py, built per recipe).

    ``need`` creates the named fixture groups that do not exist yet; ``ids`` and
    ``names`` hold the created objects' ids and the display strings to look for.
    """

    ids: Mapping[str, str]
    names: Mapping[str, str]

    def need(self, *groups: str) -> None: ...


@dataclasses.dataclass(frozen=True)
class ReadCase:
    """How to read one operation for a fixture, check the content, and check not-found.

    ``expect(result, world)`` raises ``AssertionError`` on a mismatch and returns None.
    ``missing`` overrides path args with ids that must not exist (``{}`` for a
    parameterless operation, whose control is ``missing_query`` and/or
    ``missing_body`` instead). ``allow_empty`` accepts a 204 (no content) for this
    case; it needs a populated case of the same operation in the same recipe, which
    must pass first (a 204 is then a real "empty", not a server that answers 204 to
    everything).
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


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return MUTATED
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def mutants(result: ApiResult) -> list[ApiResult]:
    """Copies of ``result`` a content check must reject.

    One with every string value replaced (body, text and header values; keys and
    numbers kept), one with no body, text or headers. A check tied to a fixture's id
    or name fails on both; one that looks only at status, keys or counts does not.
    """
    body = _scrub(result.body)
    text = json.dumps(body) if result.body is not None else (MUTATED if result.text else "")
    scrubbed = dataclasses.replace(
        result, body=body, text=text, headers={k: MUTATED for k in result.headers})
    emptied = dataclasses.replace(result, body=None, text="", headers={})
    return [scrubbed, emptied]


def _run_expect(case: ReadCase, result: ApiResult, world: World) -> None:
    returned = case.expect(result, world)
    if returned is not None:
        raise AssertionError(
            f"expect returned {returned!r} instead of raising: use assert, contains or "
            "json_path so a mismatch fails")
    for mutant in mutants(result):
        try:
            case.expect(mutant, world)
        except Exception:  # noqa: BLE001 - any failure means the check saw the change
            continue
        raise AssertionError(
            "expect does not depend on the response: it still passes with the response's "
            "strings replaced or the response emptied; check a fixture's id or name")


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


def run_case(ctx: VerifyContext, world: World, case: ReadCase, *,
             populated: frozenset[str] | set[str] = frozenset()) -> None:
    """Run one case's positive call, content check and not-found control.

    ``populated`` names the operations whose populated case already passed; a 204
    counts for an ``allow_empty`` case only if its operation is in it (standalone,
    nothing vouches for it: fail closed). On a 204 there is no content: ``expect``
    is not called, and the control still must answer not-found. HEAD and OPTIONS
    have no body: ``expect`` checks ``result.headers``, and mutants scrub those.
    """
    _run_case(ctx, world, case, populated)


def _run_case(ctx: VerifyContext, world: World, case: ReadCase,
              populated: frozenset[str] | set[str]) -> ApiResult:
    spec = OPERATIONS[case.op]
    args = dict(_resolve(case.path_args, world) or {})
    query = _resolve(case.query, world)
    accept = case.accept or (spec.accepts[0] if spec.accepts else None)
    try:
        result = _call(ctx, spec, args, query, accept, case.body)
    except FSHTTPError as e:
        raise AssertionError(f"positive call failed: {_snippet(e)}") from None
    allowed = accepted_statuses(case.op, allow_empty=case.allow_empty)
    if result.status not in allowed:
        raise AssertionError(
            f"positive call answered {result.status}, not one of {sorted(allowed)}")
    if result.status == 204:
        if case.op not in populated:
            raise AssertionError(
                f"204 (empty) accepted only after a populated {case.op} case passed")
    else:
        problem = (None if spec.http_method in ("HEAD", "OPTIONS")
                   else media_type_problem(spec, accept, result.content_type))
        if problem:
            raise AssertionError(problem)
        _run_expect(case, result, world)
    _control(ctx, spec, case, args, query, accept)
    return result


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
            return
        raise AssertionError(f"not-found control answered {e.status}, not one of "
                             f"{list(case.missing_statuses)}: {_snippet(e)}") from None
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"not-found control did not reach the server: "
                             f"{type(e).__name__}: {_snippet(e)}") from None
    raise AssertionError(f"not-found control answered {answer.status} for {bad_args or ''}"
                         f"{bad_query or ''}; a server that answers everything verifies nothing")


# ------------------------------------------------------------------ recipes


def _case_problems(case: Any) -> list[str]:
    if not isinstance(case, ReadCase):
        return [f"{case!r} is not a ReadCase"]
    spec = OPERATIONS.get(case.op)
    if spec is None:
        return [f"{case.op}: unknown operation"]
    problems = []
    if spec.kind != "read" or is_write(spec):
        problems.append(f"{case.op}: not a read operation (kind={spec.kind})")
    if not callable(case.expect):
        problems.append(f"{case.op}: expect (a content check) is required and must be callable")
    if not callable(case.path_args):
        problems.append(f"{case.op}: path_args must be a callable taking the world")
    if case.accept is not None and case.accept not in spec.accepts:
        problems.append(f"{case.op}: accept {case.accept!r} not one of {list(spec.accepts)}")
    if not isinstance(case.missing, dict):
        problems.append(f"{case.op}: missing must be a dict")
    else:
        extra = sorted(set(case.missing) - set(spec.path_params))
        if extra:
            problems.append(f"{case.op}: missing keys {extra} are not path parameters")
        if spec.path_params and not case.missing:
            problems.append(f"{case.op}: missing must override at least one of "
                            f"{list(spec.path_params)} for the not-found control")
    if not spec.path_params and case.missing_query is None and case.missing_body is None:
        problems.append(f"{case.op}: a parameterless operation needs missing_query "
                        "(or missing_body) for the not-found control")
    if case.missing_query is not None:
        absent = [q for q in spec.required_query if q not in case.missing_query]
        if absent:
            problems.append(f"{case.op}: missing_query lacks required {absent}; the client "
                            "would refuse it before the server sees it")
    if spec.body == "none" and (case.body is not None or case.missing_body is not None):
        problems.append(f"{case.op}: takes no body")
    statuses = case.missing_statuses
    if (not statuses or not all(isinstance(s, int) and 400 <= s < 500 for s in statuses)
            or set(statuses) & _UNINFORMATIVE):
        problems.append(f"{case.op}: missing_statuses must be 4xx not-found statuses "
                        f"(not {sorted(_UNINFORMATIVE)}), got {statuses!r}")
    if not all(isinstance(g, str) and g.strip() for g in case.needs):
        problems.append(f"{case.op}: needs must be non-empty group names")
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


def reads_recipe(name: str, cases: Sequence[ReadCase], *,
                 world_factory: Callable[[VerifyContext], World] = default_world) -> Recipe:
    """A ``read-content`` recipe proving each case's operation.

    Raises ValueError at construction for a non-read or unknown operation, a case
    without a callable ``expect``, an operation repeated other than as
    ``allow_empty`` variants, or a case whose not-found control cannot be built.
    ``run`` builds the world with the union of the cases' ``needs``, runs populated
    cases before ``allow_empty`` ones and raises one AssertionError listing every
    failing case. The world's fixtures live in ``ctx.fixtures``, which the runner
    tears down strictly.
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
        populated: set[str] = set()
        failures = []
        for case in ordered:
            try:
                _run_case(ctx, world, case, frozenset(populated))
            except Exception as e:  # noqa: BLE001 - collect every failing case
                kind = "" if isinstance(e, AssertionError) else f"{type(e).__name__}: "
                failures.append(f"{case.op}: {kind}{_snippet(e)}")
                continue
            if not case.allow_empty:
                populated.add(case.op)
        if failures:
            raise AssertionError(f"{len(failures)} of {len(cases)} read cases failed:\n  "
                                 + "\n  ".join(failures))

    return Recipe(name, ops, "read-content", run)
