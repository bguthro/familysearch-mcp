"""Recipes, the beta-only verifying API and the negative-control helper."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from familysearch.api import OPERATIONS, FamilySearchApi
from familysearch.api._runtime import ApiResult
from familysearch.api.spec import OpSpec, is_write
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError

EVIDENCE = ("state-change", "read-content")


def evidence_problem(ops: Iterable[str], evidence: str) -> str | None:
    """Why ``evidence`` cannot vouch for ``ops``, or None.

    ``read-content`` is only for recipes whose ops are all reads: any write (or
    unknown op) must be proven by ``state-change``.
    """
    if evidence not in EVIDENCE:
        return f"evidence must be one of {EVIDENCE}, got {evidence!r}"
    if evidence != "state-change":
        writes = [op for op in ops if op not in OPERATIONS or OPERATIONS[op].kind != "read"]
        if writes:
            return (f"ops {', '.join(writes)} are not reads; they need state-change "
                    "evidence, not read-content")
    return None


def reads_only(ops: Iterable[str]) -> bool:
    """Every op is a known read that is not a write (read POSTs count as reads)."""
    ops = tuple(ops)
    return bool(ops) and all(op in OPERATIONS and not is_write(OPERATIONS[op]) for op in ops)


def called_writes(ops: Iterable[str], called: Iterable[str]) -> list[str]:
    """The writes in ``called`` when ``ops`` are all reads (a read recipe calls no write)."""
    if not reads_only(ops):
        return []
    return sorted(op for op in called if op not in OPERATIONS or is_write(OPERATIONS[op]))


@dataclass(frozen=True)
class Recipe:
    """``ops`` are the operations this recipe proves; ``evidence`` says how.

    A ``read-content`` recipe is accepted only if ``tools.verify.reads.reads_recipe``
    built this very object (``reads.read_recipe_problem``); there is no flag to set.
    """

    name: str
    ops: tuple[str, ...]
    evidence: str
    run: Callable[[VerifyContext], None]


def _require_beta(client) -> None:
    if getattr(client, "environment", None) is not Environment.BETA:
        raise FSError("verification runs against FamilySearch beta only; refusing this client")


class VerifyApi(FamilySearchApi):
    """FamilySearchApi that unlocks the recipe's declared writes on beta, and only there.

    For a write in ``ops`` it passes ``allow_unverified`` and
    ``confirm_destructive``; for any other write it forces both False, whatever the
    caller passed; it never passes ``allow_production``. The lock still
    runs for every write, so unverifiable and excluded operations stay refused, as
    does any undeclared unverified write. With ``read_only`` (a recipe whose ops are
    all reads) every write is refused before the lock. ``called`` holds every
    operation that completed without raising.
    """

    def __init__(self, client: FSClient, *, ops: Iterable[str], registry: dict | None = None,
                 read_only: bool = False):
        _require_beta(client)
        super().__init__(client, registry=registry)
        self._unlocked = frozenset(ops)
        self.read_only = read_only
        self.called: set[str] = set()

    def _call(self, spec: OpSpec, path_args: Mapping[str, Any], **kwargs) -> ApiResult:
        _require_beta(self._client)
        # A read-only recipe's API sends no write at all, even one the lock would pass
        # (a verified write is unlocked for everyone).
        if self.read_only and is_write(spec):
            raise FSError(f"{spec.operation_id} is a write; this read-only recipe "
                          "may call only read operations")
        # The caller's flags never count: unlocking is decided here and only here.
        unlock = spec.kind != "read" and spec.operation_id in self._unlocked
        kwargs["allow_production"] = False
        kwargs["allow_unverified"] = unlock
        kwargs["confirm_destructive"] = unlock
        result = super()._call(spec, path_args, **kwargs)
        self.called.add(spec.operation_id)
        return result


# The signed-in account's identifiers never reach a report (ruling I-4): a user id
# (``cis.user.…``), the uid segment of a ``/sources/{uid}/…`` path, an agent id.
_ACCOUNT = (
    (re.compile(r"cis\.user\.[\w.-]+", re.I), "cis.user.<redacted>"),
    (re.compile(r"/sources/(?!collections\b|descriptions\b)[^/?#\s\"']+/"), "/sources/{uid}/"),
    (re.compile(r"/agents/[^/?#\s\"']+"), "/agents/<redacted>"),
)


def redact_account(text: str) -> str:
    """``text`` with the signed-in account's identifiers replaced (see ``_ACCOUNT``)."""
    for pattern, replacement in _ACCOUNT:
        text = pattern.sub(replacement, text)
    return text


# Keys whose whole subtree is account data (the signed-in user's agent record, who
# contributed or created an object, account names, emails): dropped from any failure
# skeleton. Fixture person names live under ``persons``/``display``/``names`` and stay.
ACCOUNT_KEYS = frozenset({
    "agents", "attribution", "contributor", "creator", "accountName", "userName",
    "displayName", "emails", "email", "contactName", "givenName", "familyName",
    "treeUserId", "cisUserId", "userId"})


def without_account_data(value: Any, extra: tuple[str, ...] = ()) -> Any:
    """``value`` (JSON-like) with every ``ACCOUNT_KEYS`` (and ``extra``) subtree removed."""
    if isinstance(value, dict):
        return {k: without_account_data(v, extra) for k, v in value.items()
                if k not in ACCOUNT_KEYS and k not in extra}
    if isinstance(value, list):
        return [without_account_data(v, extra) for v in value]
    return value


def expect_changed(before: Any, after: Any, what: str) -> None:
    """The negative control: a write that left its read-back unchanged proves nothing."""
    if before == after:
        raise AssertionError(f"negative control failed: {what} did not change")


@dataclass
class VerifyContext:
    api: VerifyApi
    fixtures: Any  # FixtureSet
    client: FSClient
    reason: str
    unique: str
    negative_controls: int = field(default=0)
    # What a passing run should still report (e.g. how many polls a case needed).
    notes: list[str] = field(default_factory=list)

    def expect_changed(self, before: Any, after: Any, what: str) -> None:
        """expect_changed, counted: a state-change recipe must pass at least one."""
        expect_changed(before, after, what)
        self.negative_controls += 1
