"""The write lock: which operations may run, where, and with which explicit flags.

``registry.json`` records, per operation id, a ``status`` (``verified``,
``unverifiable`` or ``excluded``), the ``fingerprint`` the verification was done
against, and a ``reason``. An operation with no entry is unverified. A verified
entry whose fingerprint no longer matches the spec is stale.
"""
import json
from pathlib import Path

from ..auth import Environment
from ..errors import FSError
from .spec import OpSpec

REGISTRY_PATH = Path(__file__).resolve().parent / "registry.json"


class OperationLockedError(FSError):
    """A write operation was refused by the lock."""


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    return json.loads(Path(path).read_text())


def effective_status(
    operation_id: str, fingerprint: str, registry: dict, impl: str = "generated"
) -> str:
    """Return verified, stale, unverifiable, excluded or unverified.

    ``impl`` names the implementation the entry applies to (an entry's optional
    ``impl`` field, default "generated"); an entry for another implementation
    does not vouch for this one.
    """
    entry = registry.get("operations", {}).get(operation_id)
    if not entry or entry.get("impl", "generated") != impl:
        return "unverified"
    status = entry.get("status")
    if status == "verified":
        return "verified" if entry.get("fingerprint") == fingerprint else "stale"
    if status in ("unverifiable", "excluded"):
        return status
    return "unverified"


def check_call(
    spec: OpSpec,
    environment: Environment,
    registry: dict,
    *,
    allow_unverified: bool = False,
    allow_production: bool = False,
    confirm_destructive: bool = False,
) -> None:
    """Raise OperationLockedError unless this call is permitted. Reads always pass."""
    if spec.kind == "read":
        return
    name = spec.operation_id
    status = effective_status(name, spec.fingerprint, registry)
    if status in ("unverifiable", "excluded"):
        entry = registry.get("operations", {}).get(name, {})
        reason = entry.get("reason") or "no reason recorded"
        raise OperationLockedError(f"{name} is {status} and can never be run: {reason}")
    if spec.destructive and not confirm_destructive:
        raise OperationLockedError(
            f"{name} is destructive; pass confirm_destructive=True to run it"
        )
    production = environment is Environment.PRODUCTION
    if status == "verified":
        if production and not allow_production:
            raise OperationLockedError(
                f"{name} is verified on beta; pass allow_production=True to run it "
                "against production"
            )
        return
    # unverified or stale
    if production:
        raise OperationLockedError(
            f"{name} is {status} and may not run against production, even with "
            "allow_unverified; verify it on beta first"
        )
    if not allow_unverified:
        raise OperationLockedError(
            f"{name} is {status}; pass allow_unverified=True to run it on beta"
        )
