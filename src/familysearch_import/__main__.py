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


def _relatives(args, repo: Path) -> int:
    import json

    from familysearch.errors import FSError
    from .planreport import load_approvals, load_links
    from .relatives import resolve_through_relatives
    from .relativesreport import relatives_json, render_relatives_report

    import_dir = args.archive / "_import"
    if is_inside(import_dir, repo):
        return _err(f"refusing to write inside the repo: {import_dir}")
    if args.max_reads is not None and args.max_reads < 0:
        return _err("--max-reads must not be negative")
    try:
        links = load_links(import_dir / "links.json")
        approvals = load_approvals(import_dir / "approvals.json")
    except FileNotFoundError:
        return _err(f"links.json not found in {import_dir}; run the links command first")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return _err(f"bad input file: {e}")
    people = scan_archive(args.archive)
    kw = {} if args.max_reads is None else {"max_reads": args.max_reads}
    try:
        result = resolve_through_relatives(people, links, approvals, _make_reader(args.env), **kw)
    except FSError as e:
        return _err(str(e))
    import_dir.mkdir(parents=True, exist_ok=True)
    report_path, json_path = import_dir / "RelativesReport.md", import_dir / "relatives.json"
    report_path.write_text(render_relatives_report(result, people), encoding="utf-8")
    json_path.write_text(json.dumps(relatives_json(result), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {report_path} and {json_path}" + (" (PARTIAL)" if result.partial else ""))
    return 0


def _relationships(args, repo: Path) -> int:
    import json

    from familysearch.errors import FSError
    from .planreport import load_approvals, load_links
    from .relgap import evaluate_edges
    from .relations import RelationsError, load_relations, to_parsed_tree
    from .relgapreport import AWAITING, FALLBACK, REVIEWED, relgap_json, render_relgap_report
    from .treeparse import parse_family_tree

    import_dir = args.archive / "_import"
    if is_inside(import_dir, repo):
        return _err(f"refusing to write inside the repo: {import_dir}")
    if args.max_reads is not None and args.max_reads < 0:
        return _err("--max-reads must not be negative")
    try:
        links = load_links(import_dir / "links.json")
        approvals = load_approvals(import_dir / "approvals.json")
    except FileNotFoundError:
        return _err(f"links.json not found in {import_dir}; run the links command first")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return _err(f"bad input file: {e}")
    people = scan_archive(args.archive)
    valid_folders = {f"{p.line}/{p.folder}" for p in people}
    parsed = {}
    sources: dict[str, str] = {}
    for line_dir in sorted({p.path.parent for p in people}, key=lambda d: nfc(d.name)):
        name = nfc(line_dir.name)
        awaiting = False
        rel_file = import_dir / "relations" / f"{line_dir.name}.json"
        if rel_file.is_file():
            try:
                rel = load_relations(rel_file, valid_folders)
            except RelationsError as e:
                return _err("; ".join(e.errors))
            if rel.reviewed:
                parsed[name] = to_parsed_tree(rel)
                sources[name] = REVIEWED
                continue
            awaiting = True
        try:
            parsed[name] = parse_family_tree(name, (line_dir / "FamilyTree.md").read_text(encoding="utf-8"))
            sources[name] = AWAITING if awaiting else FALLBACK
        except FileNotFoundError:
            print(f"note: no FamilyTree.md for line {name}")
        except (OSError, UnicodeDecodeError):
            print(f"note: could not read FamilyTree.md for line {name}")
    kw = {} if args.max_reads is None else {"max_reads": args.max_reads}
    try:
        result = evaluate_edges(parsed, people, links, approvals, _make_reader(args.env), sources=sources, **kw)
    except FSError as e:
        return _err(str(e))
    import_dir.mkdir(parents=True, exist_ok=True)
    report_path, json_path = import_dir / "RelationshipReport.md", import_dir / "relationships.json"
    report_path.write_text(render_relgap_report(result), encoding="utf-8")
    data = relgap_json(result)
    data["env"] = args.env  # tree-plan refuses a file built against another environment
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {report_path} and {json_path}" + (" (PARTIAL)" if result.partial else ""))
    return 0


def _relations_check(args, repo: Path) -> int:
    """Validate every relations file (no network, nothing written) and cross-check the regex parser."""
    from .relations import RelationsError, cross_check, load_relations
    from .treeparse import parse_family_tree

    rel_dir = args.archive / "_import" / "relations"
    files = sorted(rel_dir.glob("*.json")) if rel_dir.is_dir() else []
    if not files:
        print(f"no relations files in {rel_dir}")
        return 0
    people = scan_archive(args.archive)
    valid_folders = {f"{p.line}/{p.folder}" for p in people}
    line_dirs = {nfc(p.path.parent.name): p.path.parent for p in people}
    bad = 0
    for f in files:
        try:
            rel = load_relations(f, valid_folders)
        except RelationsError as e:
            bad += 1
            for msg in e.errors:
                print(f"error: {msg}")
            continue
        print(f"ok: {f.name}: {len(rel.nodes)} nodes, {len(rel.edges)} edges, "
              + ("reviewed" if rel.reviewed else "warning: awaiting review (not used yet)"))
        line_dir = line_dirs.get(nfc(rel.line))
        if line_dir is None:
            print(f"warning: {f.name}: no line folder named {rel.line!r} in the archive")
            continue
        try:
            tree = parse_family_tree(rel.line, (line_dir / "FamilyTree.md").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            print(f"warning: {f.name}: no readable FamilyTree.md to cross-check")
            continue
        for item in cross_check(rel, tree):
            print(f"warning: {f.name}: the regex parser found deceased {item} that the file lacks")
    return 2 if bad else 0


def _relations_review(args, repo: Path) -> int:
    from .relations import RelationsError, load_relations
    from .relationsreview import render_review

    import_dir = args.archive / "_import"
    if is_inside(import_dir, repo):
        return _err(f"refusing to write inside the repo: {import_dir}")
    rel_dir = import_dir / "relations"
    people = scan_archive(args.archive)
    valid = {f"{p.line}/{p.folder}" for p in people}
    years = {f"{p.line}/{p.folder}": (p.birth, p.death) for p in people}
    entries = []
    for f in sorted(rel_dir.glob("*.json")) if rel_dir.is_dir() else []:
        try:
            entries.append((f.stem, load_relations(f, valid)))
        except RelationsError as e:
            entries.append((f.stem, e.errors))
    import_dir.mkdir(parents=True, exist_ok=True)
    path = import_dir / "RelationsReview.md"
    path.write_text(render_review(entries, years), encoding="utf-8")
    good = [e for _, e in entries if not isinstance(e, list)]
    print(f"wrote {path}: {len(entries)} file(s), {len(entries) - len(good)} invalid, "
          f"{sum(1 for e in good if e.reviewed)} reviewed")
    return 0


def _relations_approve(args, repo: Path) -> int:
    import json

    from .relations import RelationsError, load_relations

    path = args.archive / "_import" / "relations" / f"{args.line}.json"
    if not path.is_file():
        return _err(f"no relations file for line {args.line!r}: {path}")
    valid = {f"{p.line}/{p.folder}" for p in scan_archive(args.archive)}
    try:
        rel = load_relations(path, valid)
    except RelationsError as e:
        return _err("refusing to approve an invalid file: " + "; ".join(e.errors))
    print("reminder: approving is your sign-off. Read RelationsReview.md for this line first.")
    if not args.yes:
        return _err("pass --yes to mark the file reviewed")
    if rel.reviewed:
        print(f"{path.name} is already reviewed")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    data["reviewed"] = True
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"marked {path.name} reviewed")
    return 0


def _plan(args, repo: Path) -> int:
    import json

    from familysearch.errors import FSError
    from .planner import build_plan
    from .planreport import (
        load_approvals, load_force_upload, load_ledger_keys, load_links, plan_json, render_plan_report,
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
    ledger = load_ledger_keys(import_dir / "ledger.jsonl", args.env)
    people = scan_archive(args.archive)
    if args.limit is not None:
        people = people[: max(args.limit, 0)]
    try:
        force = load_force_upload(import_dir / "approvals.json")
        plan = build_plan(people, links, approvals, _make_reader(args.env), ledger, force_upload=force, env=args.env)
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


def _tree_plan(args, repo: Path) -> int:
    import json

    from .planreport import load_links
    from .treeplan import build_tree_plan, load_tree_approvals, write_tree_plan

    import_dir = args.archive / "_import"
    if is_inside(import_dir, repo):
        return _err(f"refusing to write inside the repo: {import_dir}")
    try:
        links = load_links(import_dir / "links.json")
        relationships = json.loads((import_dir / "relationships.json").read_text(encoding="utf-8"))
        approvals = load_tree_approvals(import_dir / "approvals.json")
    except FileNotFoundError as e:
        return _err(f"{Path(e.filename).name} not found in {import_dir}; run the earlier commands first")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return _err(f"bad input file: {e}")
    try:
        plan = build_tree_plan(relationships, approvals, links, args.env)
    except ValueError as e:
        return _err(str(e))
    path = write_tree_plan(args.archive, plan)
    print(f"wrote {path} and its report (plan hash {plan.plan_hash})")
    return 0


def _print_outcomes(summary, full_keys: bool = False) -> None:
    for o in summary.outcomes:
        print(f"{o.status}: {o.kind} {o.person} [{o.key if full_keys else o.key[:8]}] {o.reason}")
    stats: dict[str, int] = {}
    for o in summary.outcomes:
        stats[o.status] = stats.get(o.status, 0) + 1
    print("summary: " + (", ".join(f"{n} {s}" for s, n in sorted(stats.items())) or "nothing to do"))
    if getattr(summary, "truncated", False):
        print("note: --max-actions reached; more actions remain")
    for w in getattr(summary, "warnings", []):
        print(f"warning: {w}", file=sys.stderr)
    if summary.stopped:
        print(f"STOPPED: {summary.stop_reason}")


def _execute(args, repo: Path) -> int:
    import requests
    from familysearch.errors import FSError
    from . import execute as ex

    plan_arg = Path(args.plan)
    plan_path = args.archive / "_import" / plan_arg if len(plan_arg.parts) == 1 else plan_arg
    try:
        ex.check_run_gates(args.env, args.allow_production, args.only, args.max_actions)
        plan = ex.verify_plan_for_execution(args.archive, plan_path, args.confirm_hash, args.env)
        ledger = ex.Ledger(args.archive / "_import" / "ledger.jsonl")

        def go() -> int:
            services = ex.build_services(args.archive, args.env, args.allow_production)
            summary = ex.run_plan(plan, services, ledger, only=args.only, max_actions=args.max_actions,
                                  execute=args.execute, allow_production=args.allow_production)
            print(("EXECUTED" if args.execute else "DRY RUN (nothing written; pass --execute)")
                  + f" env={args.env} plan={plan.plan_hash[:12]}")
            _print_outcomes(summary)
            return 3 if summary.stopped else 0

        if args.execute:
            with ex.RunLock(args.archive, break_lock=args.break_lock):
                return go()
        return go()
    except ex.GateError as e:
        return _err(str(e))
    except (FSError, requests.RequestException) as e:
        # type and HTTP status only: never str(e), which can carry response text
        return _err(f"FamilySearch session/environment check failed: {ex._http_status(e)}")


def _rollback(args, repo: Path) -> int:
    import requests
    from familysearch.errors import FSError
    from . import execute as ex

    ledger_path = args.archive / "_import" / "ledger.jsonl"
    try:
        ex.check_run_gates(args.env, args.allow_production, args.only, 1)
        if not ledger_path.is_file():
            raise ex.GateError(f"no ledger at {ledger_path}")
        ledger = ex.Ledger(ledger_path)

        def go() -> int:
            services = ex.build_services(args.archive, args.env, args.allow_production)
            summary = ex.rollback(ledger, services, args.key, args.execute,
                                  only=args.only, allow_production=args.allow_production)
            print(("ROLLED BACK" if args.execute else "DRY RUN (nothing deleted; pass --execute)")
                  + f" env={args.env}")
            _print_outcomes(summary)
            return 3 if summary.stopped or any(o.status in ("rollback-failed", "cannot-rollback") for o in summary.outcomes) else 0

        if args.execute:
            with ex.RunLock(args.archive, break_lock=getattr(args, "break_lock", False)):
                return go()
        return go()
    except ex.GateError as e:
        return _err(str(e))
    except (FSError, requests.RequestException) as e:
        # type and HTTP status only: never str(e), which can carry response text
        return _err(f"FamilySearch session/environment check failed: {ex._http_status(e)}")


def _resolve(args, repo: Path) -> int:
    import requests
    from familysearch.errors import FSError
    from . import execute as ex

    ledger_path = args.archive / "_import" / "ledger.jsonl"
    try:
        ex.check_run_gates(args.env, args.allow_production, args.only, 1)
        if not ledger_path.is_file():
            raise ex.GateError(f"no ledger at {ledger_path}")
        ledger = ex.Ledger(ledger_path)

        def go() -> int:
            services = ex.build_services(args.archive, args.env, args.allow_production)
            summary = ex.resolve(ledger, services, args.key, args.execute,
                                  only=args.only, allow_production=args.allow_production)
            print(("RESOLVED" if args.execute else "DRY RUN (nothing recorded; pass --execute)")
                  + f" env={args.env}")
            _print_outcomes(summary)
            return 3 if summary.stopped or any(o.status == "still-blocked" for o in summary.outcomes) else 0

        if args.execute:
            with ex.RunLock(args.archive, break_lock=getattr(args, "break_lock", False)):
                return go()
        return go()
    except ex.GateError as e:
        return _err(str(e))
    except (FSError, requests.RequestException) as e:
        # type and HTTP status only: never str(e), which can carry response text
        return _err(f"FamilySearch session/environment check failed: {ex._http_status(e)}")


def _tree_run(args, repo: Path, body) -> int:
    import requests
    from familysearch.errors import FSError
    from . import execute as ex
    from . import treeexec as te

    try:
        body_ctx = body(ex, te)
        return body_ctx
    except ex.GateError as e:
        return _err(str(e))
    except (FSError, requests.RequestException) as e:
        # type and HTTP status only: never str(e), which can carry response text
        return _err(f"FamilySearch session/environment check failed: {ex._http_status(e)}")


def _tree_locked(args, ex, go) -> int:
    if args.execute:
        with ex.RunLock(args.archive, break_lock=getattr(args, "break_lock", False)):
            return go()
    return go()


def _tree_execute(args, repo: Path) -> int:
    def body(ex, te) -> int:
        plan_arg = Path(args.plan)
        plan_path = args.archive / "_import" / plan_arg if len(plan_arg.parts) == 1 else plan_arg
        te.check_tree_run_gates(args.env, args.allow_production, args.only, args.max_actions)
        plan = te.verify_tree_plan_for_execution(args.archive, plan_path, args.confirm_hash, args.env)
        ledger = ex.Ledger(te.tree_ledger_path(args.archive))

        def go() -> int:
            services = te.build_tree_services(args.archive, args.env, args.allow_production)
            summary = te.run_tree_plan(plan, services, ledger, only=args.only, max_actions=args.max_actions,
                                       execute=args.execute, allow_production=args.allow_production)
            print(("EXECUTED" if args.execute else "DRY RUN (nothing written; pass --execute)")
                  + f" env={args.env} plan={plan.plan_hash[:12]}")
            _print_outcomes(summary, True)
            return 3 if summary.stopped else 0
        return _tree_locked(args, ex, go)
    return _tree_run(args, repo, body)


def _tree_rollback(args, repo: Path) -> int:
    def body(ex, te) -> int:
        te.check_tree_run_gates(args.env, args.allow_production, args.only, 1)
        path = te.tree_ledger_path(args.archive)
        if not path.is_file():
            raise ex.GateError(f"no tree ledger at {path}")
        ledger = ex.Ledger(path)

        def go() -> int:
            services = te.build_tree_services(args.archive, args.env, args.allow_production)
            summary = te.rollback_tree(ledger, services, args.key, args.execute,
                                       only=args.only, allow_production=args.allow_production)
            print(("ROLLED BACK" if args.execute else "DRY RUN (nothing deleted; pass --execute)")
                  + f" env={args.env}")
            _print_outcomes(summary, True)
            bad = any(o.status in ("rollback-failed", "cannot-rollback") for o in summary.outcomes)
            return 3 if summary.stopped or bad else 0
        return _tree_locked(args, ex, go)
    return _tree_run(args, repo, body)


def _tree_resolve(args, repo: Path) -> int:
    def body(ex, te) -> int:
        te.check_tree_run_gates(args.env, args.allow_production, args.only, 1)
        path = te.tree_ledger_path(args.archive)
        if not path.is_file():
            raise ex.GateError(f"no tree ledger at {path}")
        ledger = ex.Ledger(path)

        def go() -> int:
            services = te.build_tree_services(args.archive, args.env, args.allow_production)
            summary = te.resolve_tree(ledger, services, args.key, args.execute,
                                      only=args.only, allow_production=args.allow_production,
                                      confirm_absent=args.confirm_absent)
            print(("RESOLVED" if args.execute else "DRY RUN (nothing recorded; pass --execute)")
                  + f" env={args.env}")
            _print_outcomes(summary, True)
            return 3 if summary.stopped or any(o.status == "still-blocked" for o in summary.outcomes) else 0
        return _tree_locked(args, ex, go)
    return _tree_run(args, repo, body)


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
    tp = sub.add_parser("tree-plan", help="plan approved couples, parent links and new people (read-only)")
    tp.add_argument("--archive", required=True, type=Path)
    tp.add_argument("--env", required=True, choices=("production", "beta"))
    rl = sub.add_parser("relatives", help="find candidates for unlinked people through relatives of linked "
                        "people (read-only)")
    rl.add_argument("--archive", required=True, type=Path)
    rl.add_argument("--env", required=True, choices=("production", "beta"))
    rl.add_argument("--max-reads", type=int, default=None)
    rg = sub.add_parser("relationships", help="compare FamilyTree diagram relationships with FamilySearch "
                        "(read-only)")
    rg.add_argument("--archive", required=True, type=Path)
    rg.add_argument("--env", required=True, choices=("production", "beta"))
    rg.add_argument("--max-reads", type=int, default=None)
    rc = sub.add_parser("relations-check", help="validate reviewed relations files and cross-check them "
                        "against the regex parser (offline)")
    rc.add_argument("--archive", required=True, type=Path)
    rv = sub.add_parser("relations-review", help="write RelationsReview.md from the relations files (offline)")
    rv.add_argument("--archive", required=True, type=Path)
    ra = sub.add_parser("relations-approve", help="mark one line's relations file reviewed (your sign-off)")
    ra.add_argument("--archive", required=True, type=Path)
    ra.add_argument("--line", required=True)
    ra.add_argument("--yes", action="store_true")
    ex = sub.add_parser("execute", help="WRITE approved documents/sources to FamilySearch (dry run by default)")
    ex.add_argument("--archive", required=True, type=Path)
    ex.add_argument("--env", required=True, choices=("production", "beta"))
    ex.add_argument("--plan", required=True, help="Plan file name in <archive>/_import (or a path inside it)")
    ex.add_argument("--confirm-hash", required=True)
    ex.add_argument("--only", action="append", default=[], metavar="LINE/FOLDER")
    ex.add_argument("--max-actions", type=int, default=10)
    ex.add_argument("--execute", action="store_true", help="actually write (default: dry run)")
    ex.add_argument("--allow-production", action="store_true")
    ex.add_argument("--break-lock", action="store_true")
    rs = sub.add_parser("resolve", help="lift the block on a failed/ambiguous key that has no object id "
                        "(checks FamilySearch; dry run by default)")
    rs.add_argument("--archive", required=True, type=Path)
    rs.add_argument("--env", required=True, choices=("production", "beta"))
    rs.add_argument("--key", action="append", required=True)
    rs.add_argument("--only", action="append", default=[], metavar="LINE/FOLDER")
    rs.add_argument("--execute", action="store_true")
    rs.add_argument("--allow-production", action="store_true")
    rs.add_argument("--break-lock", action="store_true")
    rb = sub.add_parser("rollback", help="delete objects this tool created (dry run by default). "
                        "A rolled-back key still counts as executed in the ledger and cannot be "
                        "re-imported without a fresh ledger decision.")
    rb.add_argument("--archive", required=True, type=Path)
    rb.add_argument("--env", required=True, choices=("production", "beta"))
    rb.add_argument("--key", action="append", default=[])
    rb.add_argument("--only", action="append", default=[], metavar="LINE/FOLDER")
    rb.add_argument("--execute", action="store_true")
    rb.add_argument("--allow-production", action="store_true")
    rb.add_argument("--break-lock", action="store_true")
    te_ = sub.add_parser("tree-execute", help="WRITE approved couples, parent links and new people to "
                         "FamilySearch (dry run by default)")
    te_.add_argument("--archive", required=True, type=Path)
    te_.add_argument("--env", required=True, choices=("production", "beta"))
    te_.add_argument("--plan", required=True, help="TreePlan file name in <archive>/_import (or a path inside it)")
    te_.add_argument("--confirm-hash", required=True)
    te_.add_argument("--only", action="append", default=[], metavar="ACTION_KEY")
    te_.add_argument("--max-actions", type=int, default=10)
    te_.add_argument("--execute", action="store_true", help="actually write (default: dry run)")
    te_.add_argument("--allow-production", action="store_true")
    te_.add_argument("--break-lock", action="store_true")
    tr_ = sub.add_parser("tree-resolve", help="lift the block on a failed/ambiguous tree key that has no id "
                         "(checks FamilySearch; dry run by default)")
    tr_.add_argument("--archive", required=True, type=Path)
    tr_.add_argument("--env", required=True, choices=("production", "beta"))
    tr_.add_argument("--key", action="append", required=True)
    tr_.add_argument("--only", action="append", default=[], metavar="ACTION_KEY")
    tr_.add_argument("--confirm-absent", action="append", default=[], metavar="KEY",
                     help="a create-person key you have CHECKED BY HAND is not on FamilySearch "
                          "(search alone never lifts a create-person block)")
    tr_.add_argument("--execute", action="store_true")
    tr_.add_argument("--allow-production", action="store_true")
    tr_.add_argument("--break-lock", action="store_true")
    tb_ = sub.add_parser("tree-rollback", help="delete relationships/people this tool created (dry run by "
                         "default)")
    tb_.add_argument("--archive", required=True, type=Path)
    tb_.add_argument("--env", required=True, choices=("production", "beta"))
    tb_.add_argument("--key", action="append", default=[])
    tb_.add_argument("--only", action="append", default=[], metavar="ACTION_KEY")
    tb_.add_argument("--execute", action="store_true")
    tb_.add_argument("--allow-production", action="store_true")
    tb_.add_argument("--break-lock", action="store_true")
    args = ap.parse_args(argv)

    repo = repo_root()
    try:
        check_archive(args.archive, repo)
    except (FileNotFoundError, ValueError) as e:
        return _err(str(e))

    if args.cmd == "tree-execute":
        return _tree_execute(args, repo)
    if args.cmd == "tree-rollback":
        return _tree_rollback(args, repo)
    if args.cmd == "tree-resolve":
        return _tree_resolve(args, repo)
    if args.cmd == "execute":
        return _execute(args, repo)
    if args.cmd == "rollback":
        return _rollback(args, repo)
    if args.cmd == "resolve":
        return _resolve(args, repo)

    if args.cmd == "links":
        return _links(args, repo)
    if args.cmd == "plan":
        return _plan(args, repo)
    if args.cmd == "tree-plan":
        return _tree_plan(args, repo)
    if args.cmd == "relatives":
        return _relatives(args, repo)
    if args.cmd == "relationships":
        return _relationships(args, repo)
    if args.cmd == "relations-review":
        return _relations_review(args, repo)
    if args.cmd == "relations-approve":
        return _relations_approve(args, repo)
    if args.cmd == "relations-check":
        return _relations_check(args, repo)

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
