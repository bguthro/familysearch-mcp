"""Recipes, the beta-only verifying API and the negative-control helper."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from familysearch.api import FamilySearchApi
from familysearch.api._runtime import ApiResult
from familysearch.api.spec import OpSpec
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError

EVIDENCE = ("state-change", "read-content")


@dataclass(frozen=True)
class Recipe:
    """``ops`` are the operations this recipe proves; ``evidence`` says how."""

    name: str
    ops: tuple[str, ...]
    evidence: str
    run: Callable[[VerifyContext], None]


def _require_beta(client) -> None:
    if getattr(client, "environment", None) is not Environment.BETA:
        raise FSError("verification runs against FamilySearch beta only; refusing this client")


class VerifyApi(FamilySearchApi):
    """FamilySearchApi that unlocks the recipe's declared writes on beta, and only there.

    For an operation in ``ops`` it passes ``allow_unverified`` and
    ``confirm_destructive``; it never passes ``allow_production``. The lock still
    runs for every write, so unverifiable and excluded operations stay refused, as
    does any undeclared unverified write. ``called`` holds every operation that
    completed without raising.
    """

    def __init__(self, client: FSClient, *, ops: Iterable[str], registry: dict | None = None):
        _require_beta(client)
        super().__init__(client, registry=registry)
        self._unlocked = frozenset(ops)
        self.called: set[str] = set()

    def _call(self, spec: OpSpec, path_args: Mapping[str, Any], **kwargs) -> ApiResult:
        _require_beta(self._client)
        kwargs["allow_production"] = False
        if spec.kind != "read" and spec.operation_id in self._unlocked:
            kwargs["allow_unverified"] = True
            kwargs["confirm_destructive"] = True
        result = super()._call(spec, path_args, **kwargs)
        self.called.add(spec.operation_id)
        return result


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

    def expect_changed(self, before: Any, after: Any, what: str) -> None:
        """expect_changed, counted: a state-change recipe must pass at least one."""
        expect_changed(before, after, what)
        self.negative_controls += 1
