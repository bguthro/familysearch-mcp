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

The ``canaries`` group is not a fixture: it adds the CANARY block's real beta standards
values to ``names`` (no request, nothing to tear down) and never shares a world with a
fixture group. ``memory`` also reads back the persona the upload made (best effort).
``memory_comment`` adds one ZZSPIKE comment to the ``memory`` (``memory_comment_text``);
it goes through ``FixtureSet`` (raw client, body P5 sends too) and goes with the memory.
``relationship_source`` attaches the ``source`` description to the ``couple`` and to the
``child_and_parents`` relationship, by the reference-create shape P3 verified on beta
(POST ``{relationship}/source-references``, raw client as the notes are posted); the
references go with their relationships at teardown.
``collection`` is a ZZSPIKE user-defined collection (a source folder) holding the
``source`` description; it has no hand-written library call, so ``FixtureSet.collection``
and ``add_to_collection`` make it with the raw client, as the notes are posted.
"""
from __future__ import annotations

import itertools
import uuid
from collections.abc import Mapping
from types import MappingProxyType

from familysearch._urls import join_path
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.errors import FSError, FSHTTPError
from familysearch.memories import Memories
from familysearch.models import FACT_BIRTH, build_fact, build_person
from familysearch.sources import Sources
from familysearch.tree import CHILD_AND_PARENTS_PATH, COUPLE_PATH, TreeWriter

from .core import _require_beta
from .fixtures import FixtureSet, unique_png

# group -> the groups it implies (built first)
GROUPS: dict[str, tuple[str, ...]] = {
    "persons": (),
    "couple": ("persons",),
    "person_note": ("persons",),
    "couple_note": ("couple",),
    "child_and_parents_note": ("couple",),
    "notes": ("person_note", "couple_note", "child_and_parents_note"),  # alias: all three
    "source": ("persons",),
    "collection": ("source",),
    "memory": ("persons",),
    "memory_comment": ("memory",),
    "relationship_source": ("couple", "source"),
    "gtree": (),
    "gperson": ("gtree",),
    "gsource": ("gtree",),
    "ugroup": (),
}
FIXTURE_GROUPS = tuple(GROUPS)
CANARY_GROUP = "canaries"
GROUPS[CANARY_GROUP] = ()

# ------------------------------------------------------------------ CANARY BLOCK
# Real beta STANDARDS reference data (places, vocabularies, name/date standards), NOT
# fixtures: nothing is created, nothing is torn down. The standards reads are checked
# against these stable values, and are MEANT TO FAIL LOUDLY if beta's reference data
# ever changes. Edit them only here. The ids below are only sent (path/query values) and
# never become world values: short numbers would match by containment. Only the anchor
# strings in CANARY_NAMES are world names. Values are copied verbatim from Task 5 live
# run 1's response skeletons (strings there are cut at 60 characters, so a long text is
# kept as a leading part: anchors and checks match by containment), except where noted.
CANARY_PLACE_DESCRIPTION = "4229303"  # Weymouth (Massachusetts Bay Colony), a Town
CANARY_PLACE_GROUP = "15"             # "Asien und Naher Osten" (62 places)
CANARY_PLACE = "1"                    # the United States (descriptions named "u s", ...)
CANARY_PLACE_TYPE = "186"             # City
CANARY_PLACE_TYPE_GROUP = "1"         # Administrative
CANARY_VOCAB_LIST = "122774"          # Vitals
CANARY_VOCAB_TERM = "123363"          # Officer-in-Chief
CANARY_VOCAB_CONCEPT = "123363"       # Census (concept ids are not term ids)
CANARY_LOCALE = "en"
CANARY_PLACE_QUERY = "Provo"          # spec example (searchForParentPlaces value)
CANARY_NAMES: Mapping[str, str] = MappingProxyType({
    "provo_full_name": "Provo, Utah, Utah, United States",  # live run 1: passed
    "place_name": "Weymouth",
    "place_child_name": "Church of the Holy Nativity",
    # Not shown verbatim: place 1 has 228 names, the skeleton showed only "u s" (too
    # short to anchor). Probe: GET /platform/places/1 (names[].value).
    "place_by_id_name": "United States of America",
    "place_group_title": "Asien und Naher Osten",  # its German title (titles[0])
    # The attribute values are empty strings: the attribute's own id is the anchor.
    "place_attribute_id": "fs-place-attribute:1217708",
    "place_type_description": "A relatively large and permanent human settlement",
    "place_type_group_description": "Administrative place types",
    "vocab_list_title": "Vitals",
    "vocab_term_label": "Officer-in-Chief",
    "vocab_concept_uri": "http://gedcomx.org/Census",
    # The first hit for string:"List" (start 0, count 20).
    "vocab_search_hit": "Use as a form subdivision under topical headings",
    "vocab_definition": "An enumeration of the inhabitants of a specific geographic",
})
# ------------------------------------------------------------------ end of CANARY BLOCK
# Each part of these is tried even if another fails: one wrong note envelope (only the
# person note's is proven on beta) must not keep the other notes from being built.
INDEPENDENT = frozenset({"notes"})
PERSONS = (("dad", "Male", "1850"), ("mom", "Female", "1852"), ("child", "Male", "1875"))
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
    ``child_name``, ``*_note_text``, ``source_title``, ``collection_title``,
    ``memory_title``). There is no
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
        self._filed = False  # the source description is in the collection
        self._commented = False  # the memory carries its comment
        self._rel_attached: set[str] = set()  # relationships the source is attached to
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
        wanted = set(groups) | self._built
        if CANARY_GROUP in wanted and wanted - {CANARY_GROUP}:
            raise ValueError("canaries never share a world with fixture groups (a fixture's "
                             "random digits could contain a canary value)")
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
            # Best effort: the reference id is optional (no read case uses it). Live
            # (Task 4 run 1) beta answered 404 to this right after the attach; a 4xx,
            # like a 204, means "not discoverable now". A 5xx still fails the build.
            try:
                resp = self._client.get(join_path(self._client.base_url + PERSONS_PATH,
                                                  child, "source-references"))
            except FSHTTPError as e:
                if not 400 <= e.status < 500:
                    raise
                return
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

    def _build_relationship_source(self) -> None:
        sd_url = join_path(self._source_collection(), self._ids["source_description"])
        for key, path, envelope in (
                ("couple", COUPLE_PATH, "relationships"),
                ("child_and_parents", CHILD_AND_PARENTS_PATH, "childAndParentsRelationships")):
            if key in self._rel_attached:
                continue
            url = join_path(join_path(self._client.base_url + path, self._ids[key]),
                            "source-references")
            self._client.post(url, json={envelope: [{"sources": [{"description": sd_url}]}]},
                              reason=self._reason)
            self._rel_attached.add(key)

    def _build_collection(self) -> None:
        if "collection" not in self._ids:
            title = self._value("collection_title", "f")
            self._ids["collection"] = self._fixtures.collection(title)  # tracked there
            self._named("collection_title", title)
        if not self._filed:
            self._fixtures.add_to_collection(self._ids["collection"],
                                             [self._ids["source_description"]])
            self._filed = True

    def _build_gtree(self) -> None:
        """A ZZSPIKE private (Genealogies) tree, made with the raw client; nothing else in
        the world touches it. The account has none of its own (live: 204)."""
        if "gtree" not in self._ids:
            title = self._value("gtree_title", "t")
            self._ids["gtree"] = self._fixtures.genealogies_tree(title)  # tracked there
            self._named("gtree_title", title)

    def _build_gperson(self) -> None:
        """A LIVING fictional person in the private tree (never in the shared tree)."""
        if "gperson" in self._ids:
            return
        given = self._value("gperson_given", "g")
        self._ids["gperson"] = self._fixtures.genealogies_person(  # tracked there
            self._ids["gtree"], given, surname=self._surname, plain=True)
        # The full name as build_person writes it (its fullText), as the persons group.
        self._named("gperson_name", f"{given} {self._surname}", "gperson_given")

    def _build_gsource(self) -> None:
        if "gsource" not in self._ids:
            title = self._value("gsource_title", "s")
            self._ids["gsource"] = self._fixtures.genealogies_source(self._ids["gtree"], title)
            self._named("gsource_title", title)

    def _build_ugroup(self) -> None:
        """A ZZSPIKE user tree group (raw client; the account has none)."""
        if "ugroup" not in self._ids:
            name = self._value("ugroup_name", "g")
            self._ids["ugroup"] = self._fixtures.user_object("group", name)  # tracked there
            self._named("ugroup_name", name)

    def _build_canaries(self) -> None:
        """Real beta reference data (the CANARY block): constants, no request at all."""
        self._names.update(CANARY_NAMES)

    def _build_memory(self) -> None:
        if "memory" not in self._ids:
            title = self._value("memory_title", "m")
            mid = self._memories.upload_to_person(
                self._ids["child"], unique_png(), filename="zzspike.png", content_type="image/png",
                title=title, reason=self._reason)
            self._fixtures.track("memory", mid)
            self._ids["memory"] = mid
            self._named("memory_title", title)
        if "memory_persona_seq" in self._ids:
            return
        # Best effort, as deep.py: the persona the upload made for the child (it goes
        # with the memory). A 204 or 4xx means "not discoverable"; a 5xx fails the build.
        url = join_path(self._client.base_url + "/platform/memories/memories",
                        self._ids["memory"], "personas")
        try:
            resp = self._client.get(url)
        except FSHTTPError as e:
            if not 400 <= e.status < 500:
                raise
            return
        try:
            body = resp.json() if resp.text.strip() else {}
        except ValueError:
            return
        persons = body.get("persons") if isinstance(body, dict) else None
        first = persons[0] if isinstance(persons, list) and persons else {}
        # Live (Task 5 run 1): a persona id is "<memory id>-<sequence>". It contains the
        # memory id, so it cannot be a world value of its own: only the sequence is
        # kept (a case joins them). Another form is not discoverable.
        persona = str(first.get("id") or "") if isinstance(first, dict) else ""
        mid = self._ids["memory"]
        seq = persona[len(mid) + 1:] if persona.startswith(mid + "-") else ""
        if seq:
            self._ids["memory_persona_seq"] = seq

    def _build_memory_comment(self) -> None:
        text = self._value("memory_comment_text", "c")
        if not self._commented:
            self._fixtures.memory_comment(self._ids["memory"], text)
            self._commented = True
        self._named("memory_comment_text", text)

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
