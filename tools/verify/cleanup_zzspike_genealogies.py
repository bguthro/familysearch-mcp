"""One-off cleanup of ZZSPIKE leftovers in the beta Genealogies (private tree) space.

    python -m tools.verify.cleanup_zzspike_genealogies --source 2:2:286671 --tree 2:2:2:MMMF-S7Q
    python -m tools.verify.cleanup_zzspike_genealogies --source-range 286671-286676 --apply

Beta offers no list of the account's own private trees or sources (``GET /genealogies/trees``
answers public collections), so the leftovers are named by id (from a failed run's
teardown messages). Dry-run by default: each object is only READ and reported. With
``--apply`` an object is deleted only if it reads back with a title (or, for a person, a name)
that starts with ``ZZSPIKE``; anything else is skipped and never written. Deletion is the
fixture teardown's own verified delete (persons, sources, then trees; polled gone-check).
Beta only. Exit 0 when clean, 1 when a delete could not be verified, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import sys

from familysearch.errors import FSHTTPError

from .fixtures import FixtureSet, entry_title, tree_entry

PREFIX = "ZZSPIKE"
# kind -> (fixture kind, collection segment)
KINDS = {"tree": ("genealogies_tree", "trees"), "source": ("genealogies_source", "sources"),
         "person": ("genealogies_person", "persons")}


def _title(kind: str, oid: str, body) -> str | None:
    """The ZZSPIKE-checkable title of an object read back, or None if it shows none."""
    if not isinstance(body, dict):
        return None
    if kind == "tree":
        entry = tree_entry(body, oid)
        return entry_title(entry) if entry else None
    if kind == "source":
        found = (body.get("sourceDescriptions") or [{}])[0]
        titles = found.get("titles") if isinstance(found, dict) else None
        return titles[0].get("value") if titles and isinstance(titles[0], dict) else None
    found = (body.get("persons") or [{}])[0]
    names = found.get("names") if isinstance(found, dict) else None
    forms = names[0].get("nameForms") if names and isinstance(names[0], dict) else None
    return (forms[0] or {}).get("fullText") if forms else None


def expand_range(text: str) -> list[str]:
    """``286671-286676`` -> the private source ids ``2:2:286671`` .. ``2:2:286676``."""
    first, _, last = text.partition("-")
    if not (first.isdigit() and last.isdigit() and int(first) <= int(last)
            and int(last) - int(first) < 200):
        raise ValueError(f"bad range {text!r}: expected N-M with N <= M, at most 200 ids")
    return [f"2:2:{n}" for n in range(int(first), int(last) + 1)]


def cleanup(client, wanted: dict[str, list[str]], *, apply: bool, out=print) -> int:
    """Read every wanted object; with ``apply`` delete the ZZSPIKE ones. Returns the exit code."""
    fixtures = FixtureSet(client, reason="ZZSPIKE cleanup of leftover private-tree fixtures")
    for kind in ("person", "source", "tree"):  # the teardown order, whatever the input
        for oid in wanted.get(kind, []):
            fixture_kind, segment = KINDS[kind]
            try:
                resp = client.get(fixtures.genealogies_url(segment, oid))
                title = _title(kind, oid, resp.json() if resp.text.strip() else None)
            except FSHTTPError as e:
                out(f"skip {kind} {oid}: not readable (HTTP {e.status})")
                continue
            if not (isinstance(title, str) and title.startswith(PREFIX)):
                out(f"SKIP {kind} {oid}: title {title!r} does not start with {PREFIX}; "
                    "never touched")
                continue
            out(f"{'delete' if apply else 'would delete'} {kind} {oid}: {title}")
            if apply:
                fixtures.track(fixture_kind, oid)
    if not apply:
        out("dry run: pass --apply to delete the objects above")
        return 0
    problems = fixtures.teardown()
    for problem in problems:
        out(f"PROBLEM {problem}")
    return 1 if problems else 0


def main(argv: list[str] | None = None, *, client=None, out=print) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.verify.cleanup_zzspike_genealogies")
    for kind in KINDS:
        parser.add_argument(f"--{kind}", action="append", default=[], metavar="ID")
    parser.add_argument("--source-range", metavar="N-M",
                        help="private sources 2:2:N .. 2:2:M")
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args(argv)
    wanted = {kind: list(getattr(args, kind)) for kind in KINDS}
    try:
        if args.source_range:
            wanted["source"] += expand_range(args.source_range)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not any(wanted.values()):
        print("error: name objects with --tree/--source/--person/--source-range",
              file=sys.stderr)
        return 2
    if client is None:
        from .runner import build_beta_client
        client = build_beta_client()
    return cleanup(client, wanted, apply=args.apply, out=out)


if __name__ == "__main__":
    raise SystemExit(main())
