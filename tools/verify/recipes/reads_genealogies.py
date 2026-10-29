"""Read recipes for the Genealogies (private tree) API: the fixture tree, its person and source.

The ``gtree`` world group is a ZZSPIKE private tree made by the recipe itself (the
account has none), ``gperson`` a LIVING fictional
person in it, ``gsource`` a source description in it. Each read sends only the id and
anchors on the fixture's name or title. The unknown-id control uses ``NO_SUCH_GENEALOGIES``
(the live id form, ``2:2:2:<id>``, unknown).

Each read is its own recipe, because every shape here is unproven on beta.
"""
from __future__ import annotations

from ..fixtures import entry_title, local_part, tree_entry, tree_size
from ..reads import ReadCase, json_path, reads_recipe
from .genealogies_common import NO_SUCH_GENEALOGIES

DEFERRED: dict[str, str] = {
    "readGenealogiesTrees": (
        "answers a list of PUBLIC collections (live: 200 with a sourceDescriptions entry "
        "'Pedigree Resource File', id 1), not the user's own trees, even after a tree was "
        "created (before any tree existed it answered 204): no fixture value can anchor "
        "it; it also takes no parameter, so no not-found control can be built. The tree "
        "recipes read a tree by id (readGenealogiesTree) instead"),
    "readGenealogiesNote": (
        "no Genealogies operation creates a note (the create/update operations take trees, "
        "persons, sources and relationships; the spec has no notes sub-resource, unlike the "
        "Family Tree's /notes), so no fixture note id exists to read; the recon could only "
        "send ids of the wrong prefix (400 'Invalid prefix for genealogies'). Embedding a "
        "note in an updateGenealogiesPerson body is untried and unproven"),
    "readGenealogiesPersonMatches": (
        "a fictional ZZSPIKE person has no record matches: the Family Tree analogue "
        "(readTreePersonMatches) answered 204 for every available person (P6 evidence), a "
        "204 carries no anchor the request did not send, and the spec restricts the "
        "operation in production to certified applications; no fixture can produce content "
        "to verify"),
    "readGenealogiesBulkMatch": (
        "the same as readGenealogiesPersonMatches: it reads the matches of several "
        "Genealogies persons, and a fictional ZZSPIKE person has none (204); recon got 400 "
        "'Unable to fetch match resolutions' for ids of the wrong prefix, and no real match "
        "can be made for a fixture"),
}


def _tree_id(world):
    return {"tid": world.ids["gtree"]}


def _expect_tree(result, world):
    """``collections`` (or the like) holds the fixture tree, by id, under its own title."""
    tid, want = world.ids["gtree"], world.names["gtree_title"]
    entry = tree_entry(result.body, tid)
    assert entry is not None, f"collections does not hold tree {tid} (by its identifiers)"
    title = entry_title(entry)
    assert title == want, f"tree {tid} is titled {title!r}, not {want!r}"
    assert tree_size(entry) == 0, f"fresh tree {tid} reports {tree_size(entry)!r} persons"


TREE_CASES = (
    ReadCase(op="readGenealogiesTree", path_args=_tree_id,
             missing={"tid": NO_SUCH_GENEALOGIES}, expect=_expect_tree,
             anchors=(lambda w: w.names["gtree_title"],), needs=("gtree",)),
)
TREE = reads_recipe("reads.genealogies.tree", TREE_CASES)

def _persons(result, world):
    """Live: the list is ids and links only. A tree holding one person lists exactly it."""
    pid = world.ids["gperson"]
    ids = [p.get("id") for p in json_path(result.body, "persons") if isinstance(p, dict)]
    assert ids == [pid], f"the tree's persons are {ids}, not exactly [{pid!r}]"


def _expect_person(result, world):
    pid, name = world.ids["gperson"], world.names["gperson_name"]
    person = json_path(result.body, "persons[0]")
    assert person.get("id") == pid, f"the answer is person {person.get('id')}, not {pid}"
    assert name in str(person.get("names")), f"person {pid} is not named {name!r}"
    assert person.get("living") is True, f"person {pid} does not read back living"


_NAME = (lambda w: w.names["gperson_name"],)
PERSONS = reads_recipe("reads.genealogies.persons", (
    ReadCase(op="readGenealogiesPersons", path_args=_tree_id,
             missing={"tid": NO_SUCH_GENEALOGIES}, expect=_persons,
             anchors=(lambda w: w.ids["gperson"],),
             needs=("gperson",)),))
PERSON = reads_recipe("reads.genealogies.person", (
    ReadCase(op="readGenealogiesPerson", path_args=lambda w: {"pid": w.ids["gperson"]},
             missing={"pid": NO_SUCH_GENEALOGIES}, expect=_expect_person, anchors=_NAME,
             needs=("gperson",)),))

def _expect_source(result, world):
    sdid, want = world.ids["gsource"], world.names["gsource_title"]
    # Live: the source 2:2:286671 is answered with the local id s_286671.
    found = [d for d in json_path(result.body, "sourceDescriptions")
             if isinstance(d, dict) and d.get("id") and local_part(d["id"]) == local_part(sdid)]
    assert len(found) == 1, f"sourceDescriptions does not hold {sdid} once"
    titles = [t.get("value") for t in found[0].get("titles") or [] if isinstance(t, dict)]
    assert titles == [want], f"description {sdid} is titled {titles!r}, not {want!r}"


SOURCE = reads_recipe("reads.genealogies.source", (
    ReadCase(op="readGenealogiesSourceDescription",
             path_args=lambda w: {"sdid": w.ids["gsource"]},
             missing={"sdid": NO_SUCH_GENEALOGIES}, expect=_expect_source,
             anchors=(lambda w: w.names["gsource_title"],), needs=("gsource",)),))

READS_GENEALOGIES = (TREE, PERSONS, PERSON, SOURCE)
