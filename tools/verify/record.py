"""Write verification results into ``registry.json`` (the ``generated`` entry only).

Entries keep the ``{"generated": {...}, "handwritten": {...}?}`` shape: a record
call replaces only ``generated``, preserves ``handwritten`` and every other entry,
and refuses an entry in any other shape (the lock would read it as unverified).
The file is rendered like tools/spec/bootstrap_registry.py (sorted keys, indent 1,
trailing newline) and replaced atomically.
"""
import json
import os
import tempfile
from pathlib import Path

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH

from .core import EVIDENCE

_ENTRY_KEYS = {"generated", "handwritten"}


def render(registry: dict) -> str:
    return json.dumps(registry, sort_keys=True, indent=1) + "\n"


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load(path: Path) -> dict:
    registry = json.loads(Path(path).read_text())
    if not isinstance(registry, dict) or not isinstance(registry.get("operations"), dict):
        raise ValueError(f"{path} is not an operation registry")
    return registry


def _write(registry: dict, path: Path) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(render(registry))
        if path.exists():  # mkstemp is 0600; keep the registry's own mode
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _set_generated(registry: dict, operation_id: str, generated: dict, *, create: bool) -> None:
    if operation_id not in OPERATIONS:
        raise ValueError(f"unknown operation {operation_id!r}")
    operations = registry["operations"]
    entry = operations.get(operation_id)
    if entry is None:
        if not create:
            raise ValueError(f"{operation_id} has no registry entry")
        entry = {}
    if not isinstance(entry, dict) or not set(entry) <= _ENTRY_KEYS:
        raise ValueError(
            f"registry entry for {operation_id} is not {{generated, handwritten}}: {entry!r}")
    new = {"generated": dict(generated)}
    if "handwritten" in entry:
        new["handwritten"] = entry["handwritten"]
    operations[operation_id] = new


def apply_verified(
    registry: dict, operation_id: str, recipe: str, evidence: str, fingerprint: str,
    commit: str, verified_on: str,
) -> None:
    """Validate and set one verified ``generated`` entry in an in-memory registry."""
    if evidence not in EVIDENCE:
        raise ValueError(f"evidence must be one of {EVIDENCE}, got {evidence!r}")
    for name, value in (("recipe", recipe), ("commit", commit), ("verified_on", verified_on)):
        if not _nonempty(value):
            raise ValueError(f"{name} must be a non-empty string")
    spec = OPERATIONS.get(operation_id)
    if spec is None:
        raise ValueError(f"unknown operation {operation_id!r}")
    if not _nonempty(fingerprint) or fingerprint != spec.fingerprint:
        raise ValueError(
            f"fingerprint for {operation_id} does not match the generated spec; refusing")
    _set_generated(registry, operation_id, {
        "status": "verified", "fingerprint": fingerprint, "recipe": recipe,
        "evidence": evidence, "commit": commit, "verified_on": verified_on,
    }, create=False)


def record_verified(
    operation_id: str, recipe: str, evidence: str, fingerprint: str, commit: str,
    verified_on: str, path: Path = REGISTRY_PATH,
) -> None:
    registry = _load(path)
    apply_verified(registry, operation_id, recipe, evidence, fingerprint, commit, verified_on)
    _write(registry, path)


def record_many_verified(
    operation_ids, recipe: str, evidence: str, commit: str, verified_on: str,
    path: Path = REGISTRY_PATH,
) -> None:
    """Record several operations at once: all are validated before anything is written."""
    registry = _load(path)
    for oid in operation_ids:
        if oid not in OPERATIONS:
            raise ValueError(f"unknown operation {oid!r}")
        apply_verified(registry, oid, recipe, evidence, OPERATIONS[oid].fingerprint,
                       commit, verified_on)
    _write(registry, path)


def record_unverifiable(operation_id: str, reason: str, path: Path = REGISTRY_PATH) -> None:
    if not _nonempty(reason):
        raise ValueError("an unverifiable classification needs a non-empty reason")
    registry = _load(path)
    _set_generated(registry, operation_id,
                   {"status": "unverifiable", "reason": reason.strip()}, create=True)
    _write(registry, path)
