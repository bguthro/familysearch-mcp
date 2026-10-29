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


LINK_FILES = ("LinkReport.md", "links.json")


def _make_reader(env: str):
    from .prod_reader import ReadOnlyFS

    return ReadOnlyFS.production() if env == "production" else ReadOnlyFS.beta()


def _links(args, repo: Path) -> int:
    import json

    from familysearch.errors import FSError
    from .linkreport import build_link_results, links_json, render_link_report, split_by_status

    import_dir = args.archive / "_import"
    out_dir = args.out or import_dir
    if is_inside(out_dir, repo):
        return _err(f"refusing to write inside the repo: {out_dir}")
    if out_dir.resolve() != import_dir.resolve():
        if is_inside(out_dir, args.archive) and not is_inside(out_dir, import_dir):
            return _err(f"refusing to write inside the archive outside _import/: {out_dir}")
        existing = [out_dir / n for n in LINK_FILES if (out_dir / n).exists()]
        if existing:
            return _err(f"refusing to overwrite existing path: {existing[0]}")
    people = scan_archive(args.archive)
    if args.limit is not None:
        people = people[: max(args.limit, 0)]
    try:
        reader = _make_reader(args.env)
        results = build_link_results(
            people,
            reader.get_fs_person,
            lambda p: reader.search_candidates(p, status="deceased"),
        )
    except FSError as e:
        return _err(str(e))
    _, counts = split_by_status(people)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "LinkReport.md").write_text(render_link_report(results, counts), encoding="utf-8")
    (out_dir / "links.json").write_text(
        json.dumps(links_json(results), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {out_dir / 'LinkReport.md'} and {out_dir / 'links.json'}")
    return 0


def _plan(args, repo: Path) -> int:
    import json

    from familysearch.errors import FSError
    from .planner import build_plan
    from .planreport import (
        load_approvals, load_ledger_keys, load_links, plan_json, render_plan_report,
    )

    import_dir = args.archive / "_import"
    if is_inside(import_dir, repo):
        return _err(f"refusing to write inside the repo: {import_dir}")
    try:
        links = load_links(import_dir / "links.json")
        approvals = load_approvals(import_dir / "approvals.json")
    except FileNotFoundError:
        return _err(f"links.json not found in {import_dir}; run the links command first")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return _err(f"bad input file: {e}")
    ledger = load_ledger_keys(import_dir / "ledger.jsonl")
    people = scan_archive(args.archive)
    if args.limit is not None:
        people = people[: max(args.limit, 0)]
    try:
        plan = build_plan(people, links, approvals, _make_reader(args.env), ledger)
    except FSError as e:
        return _err(str(e))
    import_dir.mkdir(parents=True, exist_ok=True)
    plan_path = import_dir / "Plan.json"
    report_path = import_dir / "PlanReport.md"
    if plan_path.exists() and (import_dir / "approvals.json").exists():
        tag = plan.plan_hash[:8]
        plan_path = import_dir / f"Plan.{tag}.json"
        report_path = import_dir / f"PlanReport.{tag}.md"
    plan_path.write_text(json.dumps(plan_json(plan), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report_path.write_text(render_plan_report(plan), encoding="utf-8")
    print(f"wrote {plan_path} and {report_path} (plan hash {plan.plan_hash})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="familysearch_import")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="write the inventory report")
    r.add_argument("--archive", required=True, type=Path)
    r.add_argument("--out", type=Path)
    d = sub.add_parser("drafts", help="write missing ImportRecord drafts for one line")
    d.add_argument("--archive", required=True, type=Path)
    d.add_argument("--line", required=True)
    k = sub.add_parser("links", help="verify candidate FamilySearch ids (read-only) and write the link report")
    k.add_argument("--archive", required=True, type=Path)
    k.add_argument("--env", required=True, choices=("production", "beta"))
    k.add_argument("--limit", type=int)
    k.add_argument("--out", type=Path, help="output directory (default: <archive>/_import)")
    pl = sub.add_parser("plan", help="dry-run plan of additive documents/sources (read-only)")
    pl.add_argument("--archive", required=True, type=Path)
    pl.add_argument("--env", required=True, choices=("production", "beta"))
    pl.add_argument("--limit", type=int)
    args = ap.parse_args(argv)

    repo = repo_root()
    try:
        check_archive(args.archive, repo)
    except (FileNotFoundError, ValueError) as e:
        return _err(str(e))

    if args.cmd == "links":
        return _links(args, repo)
    if args.cmd == "plan":
        return _plan(args, repo)

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
