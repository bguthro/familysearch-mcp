"""The P2 deferred-reads report is current and accounts for every read exactly once."""
import re

from familysearch.api.registry import effective_status
from tools.spec.coverage import load_inputs
from tools.verify import deferred_report as dr


def _deferred_rows():
    text = dr.REPORT_PATH.read_text()
    section = text.split("## Deferred reads")[1].split("## Verified reads")[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")][1:]
    return [re.split(r"(?<!\\)\|", r.strip("| ")) for r in rows]


def test_committed_file_equals_generated_output():
    assert dr.REPORT_PATH.read_text() == dr.build_report()


def test_every_read_is_verified_or_deferred_exactly_once():
    ops, _ex, _areas, registry = load_inputs()
    listed = [cells[0].strip() for cells in _deferred_rows()]
    assert len(listed) == len(set(listed))
    for o in ops:
        if o.kind != "read":
            continue
        verified = effective_status(o.operation_id, o.fingerprint, registry) == "verified"
        assert verified != (o.operation_id in listed), o.operation_id
    reads = {o.operation_id for o in ops if o.kind == "read"}
    assert set(listed) <= reads


def test_every_deferred_reason_and_tier_is_non_empty():
    for cells in _deferred_rows():
        assert len(cells) == 4, cells
        assert cells[2].strip(), cells[0]
        assert re.fullmatch(r"P[3-9]", cells[3].strip()), cells[0]


def test_private_tree_reads_use_the_p7_reason():
    for cells in _deferred_rows():
        if cells[1].strip() in dr.PRIVATE_TREE_AREAS:
            assert cells[2].strip() == dr.PRIVATE_TREE_REASON


def test_claimed_unverified_reads_await_their_live_run_and_carry_no_deferral():
    claimed = {op for recipe in dr.RECIPES.values() for op in recipe.ops}
    assert not claimed & set(dr.DEFERRED)  # a read with a case is not "deferred" too
    for cells in _deferred_rows():
        op, reason, tier = cells[0].strip(), cells[2].strip(), cells[3].strip()
        if op in claimed:
            assert "awaiting its live run" in reason and tier == dr.TIERS[op], op
        elif cells[1].strip() not in dr.PRIVATE_TREE_AREAS:
            assert reason == dr._cell(dr.DEFERRED[op]), op
