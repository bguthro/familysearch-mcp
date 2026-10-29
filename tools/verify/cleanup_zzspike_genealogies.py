"""One-off cleanup of ZZSPIKE leftovers in the beta Genealogies (private tree) space.

    python -m tools.verify.cleanup_zzspike_genealogies --source 2:2:286671 --tree 2:2:2:MMMF-S7Q
    python -m tools.verify.cleanup_zzspike_genealogies --source-range 286671-286676 --apply

Beta offers no list of the account's own private trees or sources (``GET /genealogies/trees``
answers public collections), so the leftovers are named by id (from a failed run's
teardown messages). Dry-run by default: each object is only READ and reported. With
``--apply`` an object is deleted only if it reads back with a title (or, for a person, a name)
that starts with ``ZZSPIKE``; anything else is skipped and never written. Deletion is the
fixture teardown's own verified delete (persons, sources, then trees; polled gone-check).
``--list-tree-persons`` also lists each ``--tree``'s persons and deletes the ZZSPIKE ones
before the tree. Only 404/410 count as already clean. Beta only. Exit 0 when clean, 1 when
a read errored (not 404/410) or a delete could not be verified, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import sys

from familysearch.errors import FSHTTPError

from .fixtures import FixtureSet, entry_title, same_id, tree_entry

PREFIX = "ZZSPIKE"
CLEAN = (404, 410)  # the only statuses that mean "already gone"
# kind -> (fixture kind, collection segment)
KINDS = {"tree": ("genealogies_tree", "trees"), "source": ("genealogies_source", "sources"),
         "person": ("genealogies_person", "persons")}


def _entry(entries, oid: str) -> dict:
    """The entry whose id is the requested one (any prefix/local form), else {}: never
    another object's entry that merely comes first."""
    for entry in entries or []:
        if (isinstance(entry, dict) and isinstance(entry.get("id"), str)
                and same_id(entry["id"], oid)):
            return entry
    return {}


def _title(kind: str, oid: str, body) -> str | None:
    """The ZZSPIKE-checkable title of an object read back, or None if it shows none."""
    if not isinstance(body, dict):
        return None
    if kind == "tree":
        entry = tree_entry(body, oid)
        return entry_title(entry) if entry else None
    if kind == "source":
        found = _entry(body.get("sourceDescriptions"), oid)
        titles = found.get("titles") if isinstance(found, dict) else None
        return titles[0].get("value") if titles and isinstance(titles[0], dict) else None
    found = _entry(body.get("persons"), oid)
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


def _tree_persons(client, fixtures, tid: str, out) -> tuple[list[str], int]:
    """The person ids a tree lists (GET trees/{tid}/persons) and the number of errors."""
    try:
        resp = client.get(fixtures.genealogies_url("trees", tid, "persons"))
        body = resp.json() if resp.text.strip() else None
    except FSHTTPError as e:
        if e.status in CLEAN:
            out(f"skip tree {tid}: persons not listed (already clean, HTTP {e.status})")
            return [], 0
        out(f"ERROR listing the persons of tree {tid}: HTTP {e.status}")
        return [], 1
    persons = body.get("persons") if isinstance(body, dict) else None
    return [str(p["id"]) for p in persons or [] if isinstance(p, dict) and p.get("id")], 0


def cleanup(client, wanted: dict[str, list[str]], *, apply: bool, out=print,
            list_tree_persons: bool = False) -> int:
    """Read every wanted object; with ``apply`` delete the ZZSPIKE ones. Returns the exit code.

    With ``list_tree_persons`` each wanted tree's persons are listed and read too, and the
    ZZSPIKE ones are deleted BEFORE the tree. Only 404/410 count as already clean; any
    other read error is reported and makes the exit code 1.
    """
    fixtures = FixtureSet(client, reason="ZZSPIKE cleanup of leftover private-tree fixtures")
    errors = 0
    wanted = {kind: list(ids) for kind, ids in wanted.items()}
    if list_tree_persons:
        wanted.setdefault("person", [])
        for tid in wanted.get("tree", []):
            ids, bad = _tree_persons(client, fixtures, tid, out)
            errors += bad
            wanted["person"] += [i for i in ids if i not in wanted["person"]]
    for kind in ("person", "source", "tree"):  # the teardown order, whatever the input
        for oid in wanted.get(kind, []):
            fixture_kind, segment = KINDS[kind]
            try:
                resp = client.get(fixtures.genealogies_url(segment, oid))
                title = _title(kind, oid, resp.json() if resp.text.strip() else None)
            except FSHTTPError as e:
                if e.status in CLEAN:
                    out(f"skip {kind} {oid}: not readable (already clean, HTTP {e.status})")
                else:
                    out(f"ERROR {kind} {oid}: not readable (HTTP {e.status}); not cleaned")
                    errors += 1
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
        return 1 if errors else 0
    problems = fixtures.teardown()
    for problem in problems:
        out(f"PROBLEM {problem}")
    return 1 if problems or errors else 0


def main(argv: list[str] | None = None, *, client=None, out=print) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.verify.cleanup_zzspike_genealogies")
    for kind in KINDS:
        parser.add_argument(f"--{kind}", action="append", default=[], metavar="ID")
    parser.add_argument("--source-range", metavar="N-M",
                        help="private sources 2:2:N .. 2:2:M")
    parser.add_argument("--list-tree-persons", action="store_true",
                        help="also list each --tree's persons and delete the ZZSPIKE ones first")
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
    return cleanup(client, wanted, apply=args.apply, out=out,
                   list_tree_persons=args.list_tree_persons)


if __name__ == "__main__":
    raise SystemExit(main())
