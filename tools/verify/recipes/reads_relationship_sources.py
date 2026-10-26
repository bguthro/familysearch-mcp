"""Read recipes for the sources attached to a relationship (P4).

The ``relationship_source`` fixture group attaches the fixture source description to the
couple and to the child-and-parents relationship (the reference-create shape P3 verified
on beta). Every case sends only the relationship id and anchors on what it did not send:
the source description's id (named by the reference) and, where the answer carries the
description, its title. A fresh relationship answered 204 in the recon, so each positive
call polls like the person -> source reads (beta's source links lag). The controls are an
unknown relationship id: the recon saw 400 ("Unable to read TF parent/child
relationship"), and 404 is documented; an empty 204 is not accepted as a control.

The couple and the child-and-parents reads are separate recipes: one failing case keeps
its whole recipe from being recorded, and the child-and-parents shape (one more envelope)
is the less sure.
"""
from __future__ import annotations

from ..reads import ReadCase, reads_recipe
from .reads_tree_persons import _both, _id
from .reads_tree_relations import (
    CAPRID, CRID, MISSING_CAPRID, MISSING_CRID, REFERENCE_EVENTUAL, _expect_description,
    _record, _tail, _title,
)

NEEDS = ("relationship_source",)
DEFERRED: dict[str, str] = {}


def _expect_relationship_cites(envelope: str, role: str):
    """``envelope`` holds the relationship ``role``, whose sources reference the fixture
    source description."""

    def expect(result, world):
        rid, sid = world.ids[role], world.ids["source_description"]
        refs = _record(result.body, envelope, rid).get("sources")
        tails = {_tail(r.get("description")) for r in refs if isinstance(r, dict)} if (
            isinstance(refs, list)) else set()
        assert sid in tails, f"the {role} {rid} does not reference the source {sid}"

    return expect


_SOURCE = _expect_description("source_description", "source_title")
_COUPLE_CITES = _expect_relationship_cites("relationships", "couple")
_CAP_CITES = _expect_relationship_cites("childAndParentsRelationships", "child_and_parents")

COUPLE_SOURCES_CASES = (
    # /sources: the reference and the description it names, under its title.
    ReadCase(op="readCoupleRelationshipSources", path_args=CRID, missing=MISSING_CRID,
             expect=_both(_SOURCE, _COUPLE_CITES), anchors=(_title(), _id("source_description")),
             needs=NEEDS, eventual=REFERENCE_EVENTUAL),
    # /source-references: the reference alone, naming the description (no title).
    ReadCase(op="readCoupleRelationshipSourceReferences", path_args=CRID,
             missing=MISSING_CRID, expect=_COUPLE_CITES,
             anchors=(_id("source_description"),), needs=NEEDS, eventual=REFERENCE_EVENTUAL),
)
COUPLE_SOURCES = reads_recipe("reads.tree.couple_sources", COUPLE_SOURCES_CASES)

CHILD_AND_PARENTS_SOURCES_CASES = (
    ReadCase(op="getChildAndParentsRelationshipSources", path_args=CAPRID,
             missing=MISSING_CAPRID, expect=_both(_SOURCE, _CAP_CITES),
             anchors=(_title(), _id("source_description")), needs=NEEDS,
             eventual=REFERENCE_EVENTUAL),
    ReadCase(op="getChildAndParentsRelationshipSourceReferences", path_args=CAPRID,
             missing=MISSING_CAPRID, expect=_CAP_CITES,
             anchors=(_id("source_description"),), needs=NEEDS, eventual=REFERENCE_EVENTUAL),
)
CHILD_AND_PARENTS_SOURCES = reads_recipe("reads.tree.child_and_parents_sources",
                                         CHILD_AND_PARENTS_SOURCES_CASES)

READS_RELATIONSHIP_SOURCES = (COUPLE_SOURCES, CHILD_AND_PARENTS_SOURCES)
