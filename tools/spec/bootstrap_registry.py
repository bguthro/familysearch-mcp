"""Bootstrap ``registry.json``: one entry per operation, all unverified to start.

Registry keys are the post-rename operation id for generated operations and the
source id for excluded ones. Each entry holds only ``generated`` and, where a
hand-written implementation has a live test, ``handwritten``.
"""
import json
from pathlib import Path

from tools.spec.resolve import SNAPSHOT_PATH, load_overrides, resolve

REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "familysearch" / "api" / "registry.json"
)

UNVERIFIABLE = {
    "updateVocabConceptDefinition": "edits shared reference data; never executed",
    "updateVocabTranslation": "edits shared reference data; never executed",
    "createPartnerAccount": "partner-only account feature; never executed",
    "updatePartnerAccount": "partner-only account feature; never executed",
}

_RT = "tests/integration/test_beta_roundtrip.py::"
_MCP = "tests/integration/test_mcp_beta.py::"
_PERSON = _RT + "test_create_addfact_delete_person"
_PARENT_CHILD = _RT + "test_create_parent_child_relationship"
_UPLOAD_LINK = _RT + "test_upload_to_person_then_link_to_second_person"
_SOURCE = _RT + "test_source_create_and_attach"
_SOURCE_LIST = _MCP + "test_source_create_attach_list"

# operation id -> live test node id that exercises its hand-written counterpart
HANDWRITTEN = {
    "readPerson": _PERSON,
    "readPersonParents": _PARENT_CHILD,
    "readPersonChildren": _PARENT_CHILD,
    "readPersonSpouses": _RT + "test_create_couple_relationship",
    "getPersonMemories": _UPLOAD_LINK,
    "readPersonSources": _SOURCE_LIST,
    "readTreePersonMatches": _MCP + "test_matches_and_record_search",
    "searchTreePersons": _RT + "test_tree_search_runs",
    "readCurrentUser": _RT + "test_read_current_user",
    "createPerson": _PERSON,
    "updatePerson": _PERSON,
    "createRelationship": _PARENT_CHILD,
    "deletePerson": _PERSON,
    "deleteCoupleRelationship": _RT + "test_create_couple_relationship",
    "deleteChildAndParentsRelationship": _PARENT_CHILD,
    "createMemories": _RT + "test_standalone_upload_is_floating_until_linked",
    "createPersonMemory": _UPLOAD_LINK,
    "createMemoryPersona": _UPLOAD_LINK,
    "deleteMemory": _UPLOAD_LINK,
    "createSourceDescription": _SOURCE,
    "deleteSourceDescription": _SOURCE,
}


def build_registry(snapshot: dict, overrides: dict) -> dict:
    ops, excluded = resolve(snapshot, overrides)
    operations = {}
    for op in ops:
        if op.operation_id in UNVERIFIABLE:
            generated = {"status": "unverifiable", "reason": UNVERIFIABLE[op.operation_id]}
        else:
            generated = {"status": "unverified"}
        operations[op.operation_id] = {"generated": generated}
    for sid, reason in excluded.items():
        operations[sid] = {"generated": {"status": "excluded", "reason": reason}}
    missing = sorted(set(UNVERIFIABLE) - set(operations))
    if missing:
        raise ValueError(f"unverifiable mapping names unknown operation: {missing}")
    for oid, recipe in HANDWRITTEN.items():
        if oid not in operations:
            raise ValueError(f"handwritten mapping names unknown operation: {oid}")
        operations[oid]["handwritten"] = {"status": "verified", "recipe": recipe}
    return {"schema": 1, "operations": operations}


def render(registry: dict) -> str:
    return json.dumps(registry, sort_keys=True, indent=1) + "\n"


def main() -> None:
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    registry = build_registry(snapshot, load_overrides())
    REGISTRY_PATH.write_text(render(registry))


if __name__ == "__main__":
    main()
