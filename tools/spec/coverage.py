"""Coverage report: how far each operation is from verified.

Run ``python -m tools.spec.coverage [--require-done]``. With ``--require-done``
the exit status is 1 unless every operation is verified, unverifiable (with a
recorded reason) or excluded (with a recorded reason).
"""
import argparse
import json
import sys
from pathlib import Path

from familysearch.api.registry import (
    REGISTRY_PATH,
    OperationLockedError,
    _impl_entry,
    effective_status,
    load_registry,
)

from .resolve import OVERRIDES_PATH, SNAPSHOT_PATH, ResolvedOp, load_overrides, resolve


def build_report(
    ops: list[ResolvedOp],
    excluded: dict,
    registry: dict,
    areas: dict | None = None,
) -> dict:
    """Count operations by area and status and list those not yet done.

    ``areas`` maps an excluded source operation id to its snapshot area.
    Anything other than verified, unverifiable or excluded (the last two
    needing a recorded reason) is pending.
    """
    areas = areas or {}
    by_area: dict[str, dict[str, int]] = {}
    pending = []

    def count(area: str, status: str) -> None:
        counts = by_area.setdefault(area, {})
        counts[status] = counts.get(status, 0) + 1

    for o in ops:
        status = effective_status(o.operation_id, o.fingerprint, registry)
        count(o.area, status)
        reason = None
        if status == "unverifiable":
            entry = _impl_entry(o.operation_id, registry, "generated")
            reason = entry.get("reason") or None
        if status == "verified" or (status == "unverifiable" and reason):
            continue
        item = {"operation_id": o.operation_id, "kind": o.kind, "status": status}
        if reason:
            item["reason"] = reason
        pending.append(item)

    for sid, reason in excluded.items():
        count(areas.get(sid, "unknown"), "excluded")
        if not reason:
            pending.append({"operation_id": sid, "kind": "unknown", "status": "excluded"})

    return {"total": len(ops) + len(excluded), "by_area": by_area, "pending": pending}


def format_report(report: dict) -> str:
    statuses = sorted({s for c in report["by_area"].values() for s in c})
    width = max([len(a) for a in report["by_area"]] + [4])
    lines = [f"Operations: {report['total']}", ""]
    lines.append("  ".join(["Area".ljust(width)] + statuses))
    for area in sorted(report["by_area"]):
        counts = report["by_area"][area]
        lines.append(
            "  ".join([area.ljust(width)] + [str(counts.get(s, 0)).rjust(len(s)) for s in statuses])
        )
    lines += ["", f"Pending ({len(report['pending'])}):"]
    for p in report["pending"]:
        extra = f" - {p['reason']}" if p.get("reason") else ""
        lines.append(f"  {p['operation_id']} [{p['kind']}] {p['status']}{extra}")
    return "\n".join(lines)


def load_inputs(
    snapshot_path: Path = SNAPSHOT_PATH,
    overrides_path: Path = OVERRIDES_PATH,
    registry_path: Path = REGISTRY_PATH,
):
    snapshot = json.loads(Path(snapshot_path).read_text())
    ops, excluded = resolve(snapshot, load_overrides(overrides_path))
    areas = {o["operation_id"]: o["area"] for o in snapshot["operations"]}
    return ops, excluded, areas, load_registry(registry_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.spec.coverage")
    parser.add_argument("--require-done", action="store_true",
                        help="exit 1 unless every operation is verified, unverifiable or excluded")
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_PATH)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    args = parser.parse_args(argv)
    try:
        ops, excluded, areas, registry = load_inputs(args.snapshot, args.overrides, args.registry)
        report = build_report(ops, excluded, registry, areas)
    except OperationLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_report(report))
    if args.require_done and report["pending"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
