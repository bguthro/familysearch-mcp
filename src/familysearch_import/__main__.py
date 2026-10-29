"""CLI: python -m familysearch_import {report,drafts} --archive PATH"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .record import DRAFT_FILE, RECORD_FILE, is_inside, repo_root, write_draft
from .record import record_state_and_status
from .report import build_report, check_archive
from .scan import nfc, scan_archive


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="familysearch_import")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="write the inventory report")
    r.add_argument("--archive", required=True, type=Path)
    r.add_argument("--out", type=Path)
    d = sub.add_parser("drafts", help="write missing ImportRecord drafts for one line")
    d.add_argument("--archive", required=True, type=Path)
    d.add_argument("--line", required=True)
    args = ap.parse_args(argv)

    repo = repo_root()
    try:
        check_archive(args.archive, repo)
    except (FileNotFoundError, ValueError) as e:
        return _err(str(e))

    if args.cmd == "report":
        import_dir = args.archive / "_import"
        default = import_dir / "InventoryReport.md"
        out = args.out or default
        if is_inside(out, repo):
            return _err(f"refusing to write inside the repo: {out}")
        if out.resolve() != default.resolve():
            if out.exists():
                return _err(f"refusing to overwrite existing path: {out}")
            if is_inside(out, args.archive) and not is_inside(out, import_dir):
                return _err(f"refusing to write inside the archive outside _import/: {out}")
        try:
            text = build_report(args.archive, repo)
        except (FileNotFoundError, ValueError) as e:
            return _err(str(e))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
        return 0

    written = skipped = 0
    for p in scan_archive(args.archive, [args.line]):
        status, state = record_state_and_status(p)
        if status != "deceased" or state != "missing" or (p.path / DRAFT_FILE).exists():
            skipped += 1
            continue
        try:
            write_draft(p, repo)
        except (FileExistsError, ValueError, OSError):
            skipped += 1
            continue
        written += 1
    print(f"drafts written: {written}, skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
