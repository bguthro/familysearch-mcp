"""The fixture world for read verification: ZZSPIKE fixtures on beta, built lazily by group.

Every object is created with the hand-written library calls the API recon proved on
beta (docs/superpowers/recon/2026-10-25-api-surface/deep.py) and tracked in the
recipe's ``FixtureSet`` on the line after it exists, so a build that fails half way
still tears down. Notes are embedded in their owner (a person or relationship that is
tracked before the note is posted) and go with it at teardown.

``ids`` and ``names`` are what a read case sees (``tools.verify.reads.seal``). Each
value belongs to one fixture: names carry their own uuid digits, the run's shared
surname and the ``ZZSPIKE`` prefix are never values on their own, and no value may be
contained in another, so an anchor can only match its own record.
"""
from __future__ import annotations

import base64
import itertools
import uuid
from collections.abc import Mapping
from types import MappingProxyType

from familysearch._urls import join_path
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.errors import FSError
from familysearch.memories import Memories
from familysearch.models import FACT_BIRTH, build_fact, build_person
from familysearch.sources import Sources
from familysearch.tree import CHILD_AND_PARENTS_PATH, COUPLE_PATH, TreeWriter

from .core import _require_beta
from .fixtures import FixtureSet

# group -> the groups it implies (built first)
GROUPS: dict[str, tuple[str, ...]] = {
    "persons": (),
    "couple": ("persons",),
    "person_note": ("persons",),
    "couple_note": ("couple",),
    "child_and_parents_note": ("couple",),
    "notes": ("person_note", "couple_note", "child_and_parents_note"),  # alias: all three
    "source": ("persons",),
    "memory": ("persons",),
}
# Each part of these is tried even if another fails: one wrong note envelope (only the
# person note's is proven on beta) must not keep the other notes from being built.
INDEPENDENT = frozenset({"notes"})
PERSONS = (("dad", "Male", "1850"), ("mom", "Female", "1852"), ("child", "Male", "1875"))
PNG = base64.b64decode(  # a 1x1 PNG, as deep.py uploads
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5E"
    "rkJggg==")
PERSONS_PATH = "/platform/tree/persons"


def _token(tag: str) -> str:
    """A per-fixture value: ``ZZSPIKE`` + an upper-case tag letter + 12 decimal digits.

    Digits only after the prefix: nothing a server could re-case in a returned name.
    """
    return f"ZZSPIKE{tag.upper()}{uuid.uuid4().int % 10**12:012d}"


class World:
    """Lazy fixture builder; see the module docstring.

    ``need(*groups)`` builds the missing groups (and what they imply) once; a retry
    after a failure creates only the objects that do not exist yet. ``notes`` builds
    ``person_note``, ``couple_note`` and ``child_and_parents_note``, each attempted even
    when another fails (the first failure is raised after all were tried).

    ``names`` holds full per-fixture strings only (``dad_name``, ``mom_name``,
    ``child_name``, ``*_note_text``, ``source_title``, ``memory_title``). There is no
    ``unique_surname``: the surname is shared by every person, so it is contained in
    each full name and could not tell them apart. A case that searches by surname
    (Task 3) sends it in the query as ``names["child_name"].split()[-1]`` and anchors on
    one person's full name (anchors must be exact world values, and a value the
    request sends cannot be the only anchor).
    """

    def __init__(self, client: FSClient, fixtures: FixtureSet, reason: str, unique: str):
        _require_beta(client)  # first, before any I/O
        if not (isinstance(reason, str) and reason.strip()):
            raise ValueError("a non-empty reason is required")
        if not (isinstance(unique, str) and unique.strip()):
            raise ValueError("a non-empty unique tag is required")
        self._client = client
        self._fixtures = fixtures
        self._reason = reason
        self._surname = unique
        self._discovery = Discovery(client)  # lazy: no request until a link is needed
        self._writer = TreeWriter(client, self._discovery)
        self._sources = Sources(client, self._discovery)
        self._memories = Memories(client, self._discovery)
        self._ids: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._pending: dict[str, str] = {}  # values for objects not created yet
        self._attached = False
        self._built: set[str] = set()

    @property
    def ids(self) -> Mapping[str, str]:
        return MappingProxyType(self._ids)

    @property
    def names(self) -> Mapping[str, str]:
        return MappingProxyType(self._names)

    def need(self, *groups: str) -> None:
        unknown = sorted(set(groups) - set(GROUPS))
        if unknown:
            raise ValueError(f"unknown fixture group(s) {', '.join(unknown)}; "
                             f"expected some of {', '.join(GROUPS)}")
        for group in groups:
            self._build(group)

    def _build(self, group: str) -> None:
        if group in self._built:
            return
        if group in INDEPENDENT:
            errors = []
            for part in GROUPS[group]:
                try:
                    self._build(part)
                except Exception as e:  # noqa: BLE001 - try the rest, then raise
                    errors.append(e)
            if errors:
                raise errors[0]
            self._built.add(group)
            return
        for implied in GROUPS[group]:
            self._build(implied)
        getattr(self, f"_build_{group}")()
        self._check_distinct()
        self._built.add(group)

    # ---------------------------------------------------------------- groups

    def _value(self, key: str, tag: str) -> str:
        """The value an object about to be created will carry (reused on a retry).

        It becomes a name (``_named``) only once the object exists.
        """
        return self._pending.setdefault(key, _token(tag))

    def _named(self, key: str, value: str, pending: str | None = None) -> None:
        """The object exists: ``value`` is now its name and no longer pending."""
        self._names[key] = value
        self._pending.pop(pending or key, None)

    def _build_persons(self) -> None:
        for role, gender, year in PERSONS:
            if role in self._ids:
                continue
            given = self._value(f"{role}_given", role[0])
            person = build_person(given=given, surname=self._surname, gender=gender,
                                  living=False, facts=[build_fact(FACT_BIRTH, date=year)])
            pid = self._writer.create_person(person, reason=self._reason)
            self._fixtures.track("person", pid)
            self._ids[role] = pid
            # The full name as build_person writes it (its fullText); the bare given
            # name and the shared surname are never values on their own.
            self._named(f"{role}_name", f"{given} {self._surname}", f"{role}_given")

    def _build_couple(self) -> None:
        ids = self._ids
        if "couple" not in ids:
            rid = self._writer.create_couple(ids["dad"], ids["mom"], reason=self._reason)
            self._fixtures.track("couple", rid)
            ids["couple"] = rid
        if "child_and_parents" not in ids:
            cid = self._writer.create_parent_child(
                child_id=ids["child"], parent_ids=[ids["dad"], ids["mom"]], reason=self._reason)
            self._fixtures.track("child_and_parents", cid)
            ids["child_and_parents"] = cid

    def _build_person_note(self) -> None:
        url = join_path(self._client.base_url + PERSONS_PATH, self._ids["child"], "notes")
        self._note("person_note", url, "persons", "p")

    def _build_couple_note(self) -> None:
        url = join_path(self._client.base_url + COUPLE_PATH, self._ids["couple"], "notes")
        self._note("couple_note", url, "relationships", "r")

    def _build_child_and_parents_note(self) -> None:
        url = join_path(self._client.base_url + CHILD_AND_PARENTS_PATH,
                        self._ids["child_and_parents"], "notes")
        self._note("child_and_parents_note", url, "childAndParentsRelationships", "a")

    def _note(self, key: str, url: str, envelope: str, tag: str) -> None:
        if key in self._ids:
            return
        text = self._value(f"{key}_text", tag)
        # The owner is tracked already: the note is embedded in it and goes with it.
        resp = self._client.post(url, json={envelope: [{"notes": [{
            "subject": f"{text} subject", "text": text}]}]}, reason=self._reason)
        self._ids[key] = self._note_id(resp)
        self._named(f"{key}_text", text)

    @staticmethod
    def _note_id(resp) -> str:
        """The new note's id: the Location's last segment, else X-ENTITY-ID (as deep.py)."""
        headers = getattr(resp, "headers", None) or {}
        loc = (headers.get("Location") or "").split("?")[0].rstrip("/")
        nid = loc.rsplit("/", 1)[-1] if loc else headers.get("X-ENTITY-ID")
        if not nid:
            raise FSError("note created but no new id was returned")
        return nid

    def _source_collection(self) -> str:
        href = self._discovery.href("source-descriptions").split("?")[0]
        return href if href.startswith("http") else self._client.base_url + href

    def _build_source(self) -> None:
        if "source_description" not in self._ids:
            title = self._value("source_title", "s")
            sid = self._sources.create_description(
                f"https://example.org/{title.lower()}", title, f"{title} citation",
                reason=self._reason)
            self._fixtures.track("source", sid)
            self._ids["source_description"] = sid
            self._named("source_title", title)
        child = self._ids["child"]
        if not self._attached:
            self._sources.attach_to_person(
                child, join_path(self._source_collection(), self._ids["source_description"]),
                reason=self._reason)
            self._attached = True
        if "source_reference" not in self._ids:
            resp = self._client.get(join_path(self._client.base_url + PERSONS_PATH, child,
                                              "source-references"))
            srid = self._source_reference(resp)
            if srid:
                self._ids["source_reference"] = srid

    def _source_reference(self, resp) -> str | None:
        """The child's reference to our description, read back as deep.py does.

        A reference naming our description wins; failing that, the only reference
        (the child is a fresh fixture, so it is ours). None if there is none.
        """
        if not resp.text.strip():
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        persons = body.get("persons") if isinstance(body, dict) else None
        first = persons[0] if isinstance(persons, list) and persons else {}
        refs = first.get("sources") if isinstance(first, dict) else None
        refs = [r for r in refs if isinstance(r, dict) and r.get("id")] if isinstance(
            refs, list) else []
        sid = self._ids["source_description"]
        for ref in refs:
            desc = str(ref.get("description") or "").split("?")[0].rstrip("/")
            if desc.rsplit("/", 1)[-1] == sid:
                return str(ref["id"])
        return str(refs[0]["id"]) if len(refs) == 1 else None

    def _build_memory(self) -> None:
        if "memory" in self._ids:
            return
        title = self._value("memory_title", "m")
        mid = self._memories.upload_to_person(
            self._ids["child"], PNG, filename="zzspike.png", content_type="image/png",
            title=title, reason=self._reason)
        self._fixtures.track("memory", mid)
        self._ids["memory"] = mid
        self._named("memory_title", title)

    # ---------------------------------------------------------------- anchors safety

    def _check_distinct(self) -> None:
        """No value may be empty, repeated or contained in another (anchors match by
        containment, so one fixture's value inside another's would let it stand in)."""
        values = [*self._ids.items(), *self._names.items()]
        for key, value in values:
            if not (isinstance(value, str) and value):
                raise FSError(f"fixture value {key!r} is empty")
        for (ka, a), (kb, b) in itertools.permutations(values, 2):
            if a in b:
                raise FSError(f"fixture value {ka}={a!r} is contained in {kb}={b!r}; "
                              "anchors could not tell these fixtures apart")
