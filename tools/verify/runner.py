"""Run verification recipes against FamilySearch BETA and record what they prove.

    python -m tools.verify list
    python -m tools.verify run (--recipe NAME ... | --all) [--dry-run]
    python -m tools.verify classify OPERATION_ID --status unverifiable --reason TEXT

``run`` refuses (exit 2) a working tree with uncommitted changes other than
``registry.json`` (the recorded commit must describe the code that ran). It then takes
the session cookie from Brave and aborts (exit 2) unless beta's
``GET /platform/users/current`` answers 200 for it. Production is never contacted
and is refused in every code path. A recipe marks its operations verified only when it
raised nothing, its fixtures were torn down cleanly, it called every operation it
claims, (for ``state-change`` evidence) passed a negative control and (when every
operation it claims is a read) called no write: such a recipe's API refuses writes
before sending them, and any write found in ``api.called`` fails the recipe.
Otherwise the registry file is left untouched. The token is never printed.
"""
import argparse
import datetime
import subprocess
import sys
import uuid
from pathlib import Path

from familysearch.api.registry import REGISTRY_PATH, load_registry
from familysearch.auth import Environment, extract_brave_token
from familysearch.client import FSClient
from familysearch.errors import FSError

from . import record
from .core import (Recipe, VerifyApi, VerifyContext, called_writes, evidence_problem,
                   reads_only)
from .fixtures import FixtureSet
from .recipes import RECIPES, validate_recipes

REPO_ROOT = Path(__file__).resolve().parents[2]
CURRENT_USER = "/platform/users/current"
REGISTRY_REL = "src/familysearch/api/registry.json"
NOT_BETA = "not a beta session (are you signed into production?)"


def make_context(client: FSClient, recipe: Recipe, *, registry_path: Path = REGISTRY_PATH,
                 reason: str | None = None) -> VerifyContext:
    """A fresh API, fixture set and unique tag for one recipe run."""
    unique = "ZZSPIKE" + uuid.uuid4().hex[:8]
    reason = reason or f"ZZSPIKE automated API verification {unique}"
    api = VerifyApi(client, ops=recipe.ops, registry=load_registry(registry_path),
                    read_only=reads_only(recipe.ops))
    return VerifyContext(api=api, fixtures=FixtureSet(client, reason=reason), client=client,
                         reason=reason, unique=unique)


def run_recipe(recipe: Recipe, ctx: VerifyContext, *, registry_path: Path, commit: str,
               today: str) -> dict:
    problems: list[str] = []
    try:
        try:
            recipe.run(ctx)
        except Exception as e:  # noqa: BLE001 - a failing recipe is a result, not a crash
            problems.append(f"run: {type(e).__name__}: {e}")
    finally:
        try:
            problems += [f"teardown: {p}" for p in ctx.fixtures.teardown()]
        except Exception as e:  # noqa: BLE001
            problems.append(f"teardown: {type(e).__name__}: {e}")
    if not recipe.ops:
        problems.append("recipe declares no ops")
    missing = sorted(set(recipe.ops) - ctx.api.called)
    if missing:
        problems.append(f"claimed but never successfully called: {', '.join(missing)}")
    if recipe.evidence == "read-content" and recipe.reads is not True:
        # Only reads_recipe's cases are held to anchors and controls, and see a sealed world.
        problems.append("read-content recipes must be built with tools.verify.reads.reads_recipe")
    writes = called_writes(recipe.ops, ctx.api.called)
    if writes:
        problems.append(f"read-only recipe called write operation(s): {', '.join(writes)}")
    evidence = evidence_problem(recipe.ops, recipe.evidence)
    if evidence:
        problems.append(evidence)
    elif recipe.evidence == "state-change" and ctx.negative_controls < 1:
        problems.append("state-change evidence needs at least one passing negative control "
                        "(ctx.expect_changed)")
    if not problems:
        try:
            record.record_many_verified(recipe.ops, recipe.name, recipe.evidence, commit, today,
                                        path=registry_path)
        except (ValueError, OSError) as e:
            problems.append(f"record: {e}")
    return {"recipe": recipe.name, "passed": not problems, "problems": problems}


def build_beta_client() -> FSClient:
    """A client for beta from Brave's cookie, or FSError. Never contacts production.

    Only beta is probed: a session that beta's current-user read does not answer
    200 for (production's, an expired one, or an unreachable host) is refused.
    """
    token = extract_brave_token()
    client = FSClient(token, Environment.BETA)
    if client.environment is not Environment.BETA:
        raise FSError("refusing a non-beta client")
    try:
        status = client.get(CURRENT_USER).status_code
    except Exception as e:  # noqa: BLE001 - 401/403/network: all mean "not usable here"
        raise FSError(f"{NOT_BETA}: {type(e).__name__}") from None
    if status != 200:
        raise FSError(f"{NOT_BETA}: beta {CURRENT_USER} answered {status}")
    return client


def registry_in_repo() -> bool:
    """The imported registry must be this repo's, or the recorded commit would lie."""
    return REPO_ROOT in Path(REGISTRY_PATH).resolve().parents


def git_status_porcelain() -> str:
    return subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=True).stdout


def dirty_paths(porcelain: str) -> list[str]:
    """Changed paths other than the registry itself (renames count as dirty)."""
    dirty = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path or path.strip('"') != REGISTRY_REL:
            dirty.append(path)
    return dirty


def current_commit() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                         text=True, check=True)
    return out.stdout.strip()


def _describe(recipe: Recipe) -> str:
    return f"{recipe.name} [{recipe.evidence}]: {', '.join(recipe.ops)}"


def _select(args) -> list[Recipe] | None:
    if args.all:
        return list(RECIPES.values())
    unknown = [n for n in args.recipe if n not in RECIPES]
    if unknown:
        print(f"error: unknown recipe(s): {', '.join(unknown)}", file=sys.stderr)
        return None
    if not args.recipe:
        print("error: name recipes with --recipe, or pass --all", file=sys.stderr)
        return None
    return [RECIPES[n] for n in args.recipe]


def _run(args) -> int:
    problems = validate_recipes(RECIPES)
    if problems:
        print("error: invalid recipes:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    selected = _select(args)
    if selected is None:
        return 2
    if not args.dry_run and not registry_in_repo():
        print(f"abort: familysearch is imported from outside this repo (registry at "
              f"{REGISTRY_PATH}); install it editable from {REPO_ROOT}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"would run {len(selected)} recipe(s) on beta:")
        for recipe in selected:
            print(f"  {_describe(recipe)}")
        return 0
    if not selected:
        print("no recipes to run")
        return 0
    try:
        dirty = dirty_paths(git_status_porcelain())
    except Exception as e:  # noqa: BLE001
        print(f"abort: cannot read git status: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if dirty:
        print("abort: uncommitted changes (commit them so the recorded commit is the code "
              "that ran):\n  " + "\n  ".join(dirty), file=sys.stderr)
        return 2
    try:
        client = build_beta_client()
        commit = current_commit()
    except Exception as e:  # noqa: BLE001 - any failure here aborts; messages carry no token
        print(f"abort: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    today = datetime.date.today().isoformat()
    failed = 0
    for recipe in selected:
        ctx = make_context(client, recipe, registry_path=args.registry)
        result = run_recipe(recipe, ctx, registry_path=args.registry, commit=commit, today=today)
        if result["passed"]:
            print(f"PASS {recipe.name}: verified {', '.join(recipe.ops)}")
        else:
            failed += 1
            print(f"FAIL {recipe.name}:")
            for p in result["problems"]:
                print(f"  {p}")
    print(f"{len(selected) - failed} passed, {failed} failed")
    return 1 if failed else 0


def _classify(args) -> int:
    try:
        record.record_unverifiable(args.operation_id, args.reason, path=args.registry,
                                   force=args.force_reclassify)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{args.operation_id}: unverifiable ({args.reason.strip()})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.verify")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the recipes and the operations each proves")
    run_p = sub.add_parser("run", help="run recipes against beta and record results")
    run_p.add_argument("--recipe", action="append", default=[], metavar="NAME")
    run_p.add_argument("--all", action="store_true")
    run_p.add_argument("--dry-run", action="store_true",
                       help="list what would run; no cookie, no network")
    run_p.add_argument("--registry", type=Path, default=REGISTRY_PATH, help=argparse.SUPPRESS)
    cls_p = sub.add_parser("classify", help="record an operation as unverifiable")
    cls_p.add_argument("operation_id")
    cls_p.add_argument("--status", required=True, choices=["unverifiable"])
    cls_p.add_argument("--reason", required=True)
    cls_p.add_argument("--force-reclassify", action="store_true",
                       help="allow replacing a verified entry (excluded ones never are)")
    cls_p.add_argument("--registry", type=Path, default=REGISTRY_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "list":
        for recipe in RECIPES.values():
            print(_describe(recipe))
        if not RECIPES:
            print("(no recipes)")
        return 0
    if args.command == "run":
        return _run(args)
    return _classify(args)
