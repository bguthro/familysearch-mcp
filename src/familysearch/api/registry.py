"""The write lock: which operations may run, where, and with which explicit flags.

``registry.json`` holds ``operations[id] = {"generated": {...}, "handwritten": {...}?}``.
Each implementation entry has a ``status`` of ``verified``, ``unverified``,
``unverifiable`` or ``excluded``; ``stale`` and ``unknown`` are computed, never
stored. A ``generated`` verified entry carries the ``fingerprint`` it was verified
against and is stale when that no longer matches the spec. ``handwritten`` entries
carry no fingerprint and are informational only: the lock reads only ``generated``.
An operation with no entry is unverified.
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


def _impl_entry(operation_id: str, registry, impl: str):
    """Return the registry entry for one implementation, or None when absent.

    Raises OperationLockedError if the registry is malformed (fail closed).
    """
    if not isinstance(registry, dict):
        raise OperationLockedError("operation registry is malformed (not an object)")
    operations = registry.get("operations")
    if not isinstance(operations, dict):
        raise OperationLockedError("operation registry is malformed (no operations map)")
    entry = operations.get(operation_id)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise OperationLockedError(f"registry entry for {operation_id} is malformed")
    impl_entry = entry.get(impl)
    if impl_entry is None:
        return None
    if not isinstance(impl_entry, dict):
        raise OperationLockedError(f"registry entry for {operation_id}/{impl} is malformed")
    return impl_entry


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value)


def effective_status(
    operation_id: str, fingerprint: str, registry: dict, impl: str = "generated"
) -> str:
    """Return verified, stale, unverifiable, excluded, unverified or unknown.

    Entries live at ``registry["operations"][id][impl]``. A verified entry is
    only honored when both its stored fingerprint and ``fingerprint`` are
    non-empty and equal; otherwise it is stale. An unrecognised status string
    is reported as "unknown" (check_call refuses writes with it).

    ``impl="handwritten"`` has no staleness semantics: handwritten entries carry
    no fingerprint, so a verified one always reports "stale" here. The lock never
    asks about them.
    """
    impl_entry = _impl_entry(operation_id, registry, impl)
    if impl_entry is None:
        return "unverified"
    status = impl_entry.get("status")
    if status == "verified":
        stored = impl_entry.get("fingerprint")
        if _nonempty(stored) and _nonempty(fingerprint) and stored == fingerprint:
            return "verified"
        return "stale"
    if status in ("unverifiable", "excluded", "unverified"):
        return status
    return "unknown"


def check_call(
    spec: OpSpec,
    environment: Environment,
    registry: dict,
    *,
    allow_unverified: bool = False,
    allow_production: bool = False,
    confirm_destructive: bool = False,
) -> None:
    """Raise OperationLockedError unless this call is permitted. Reads always pass.

    Fails closed: only the literal Environment.BETA counts as beta, a flag counts
    only when it is exactly True, and an operation counts as non-destructive only
    when ``spec.destructive is False``.
    """
    if spec.kind == "read":
        return
    name = spec.operation_id
    status = effective_status(name, spec.fingerprint, registry)
    if status == "unknown":
        raw = _impl_entry(name, registry, "generated").get("status")
        raise OperationLockedError(
            f"{name} has an unrecognised registry status {raw!r}; refusing to run it"
        )
    if status in ("unverifiable", "excluded"):
        reason = _impl_entry(name, registry, "generated").get("reason") or "no reason recorded"
        raise OperationLockedError(f"{name} is {status} and can never be run: {reason}")
    if spec.destructive is not False and confirm_destructive is not True:
        raise OperationLockedError(
            f"{name} is destructive; pass confirm_destructive=True to run it"
        )
    beta = environment is Environment.BETA
    if status == "verified":
        if not beta and allow_production is not True:
            raise OperationLockedError(
                f"{name} is verified on beta; pass allow_production=True to run it "
                "against production"
            )
        return
    # unverified or stale
    if not beta:
        raise OperationLockedError(
            f"{name} is {status} and may not run against production, even with "
            "allow_unverified; verify it on beta first"
        )
    if allow_unverified is not True:
        raise OperationLockedError(
            f"{name} is {status}; pass allow_unverified=True to run it on beta"
        )
