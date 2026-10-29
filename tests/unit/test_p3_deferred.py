"""The P3 deferred-writes report is current and accounts for every Family Tree write."""
import re

import pytest

from familysearch.api.registry import effective_status
from tools.spec.coverage import load_inputs
from tools.verify import deferred_report as dr


def _section(name, until):
    text = dr.P3_REPORT_PATH.read_text()
    section = text.split(name)[1].split(until)[0] if until else text.split(name)[1]
    rows = [line for line in section.splitlines() if line.startswith("| ")][1:]
    return [[c.strip() for c in re.split(r"(?<!\\)\|", r.strip("| "))] for r in rows]


def _unverified_rows():
    return _section("## Unverified writes", "## Verified writes")


def _writes():
    ops, _ex, _areas, registry = load_inputs()
    return {o.operation_id: effective_status(o.operation_id, o.fingerprint, registry)
            for o in ops if o.kind != "read" and o.area == dr.P3_AREA}


def test_committed_file_equals_generated_output():
    assert dr.P3_REPORT_PATH.read_text() == dr.build_p3_report()


def test_check_passes_on_the_committed_reports():
    assert dr.main(["--check"]) == 0


def test_check_fails_on_a_stale_report(tmp_path):
    stale = tmp_path / "p3.md"
    stale.write_text("old\n")
    assert dr.main(["--check", "--p3-path", str(stale)]) == 1


def test_every_family_tree_write_is_verified_or_listed_exactly_once():
    listed = [cells[0] for cells in _unverified_rows()]
    assert len(listed) == len(set(listed))
    writes = _writes()
    for op, status in writes.items():
        if status == "unverifiable":  # never run (e.g. mergePerson): not pending work
            assert op not in listed, op
            continue
        assert (status == "verified") != (op in listed), op
    verified = {op for cells in _section("## Verified writes by recipe", None)
                for op in cells[1].split(", ")}
    assert verified == {op for op, s in writes.items() if s == "verified"}


def test_every_reason_and_tier_is_non_empty():
    for cells in _unverified_rows():
        assert len(cells) == 3, cells
        assert cells[1], cells[0]
        assert re.fullmatch(r"P[3-9]", cells[2]), cells[0]


def test_claimed_writes_await_their_live_run_and_the_rest_name_a_later_tier():
    claimed = {op for recipe in dr.RECIPES.values() for op in recipe.ops}
    for op, reason, tier in _unverified_rows():
        if op in dr.WRITE_DEFERRED:
            assert tier != "P3" and reason == dr._cell(dr.WRITE_DEFERRED[op])
        elif op in claimed:
            assert tier == dr.awaiting_tier(op) and "awaiting its live run" in reason, op
        else:
            assert tier != "P3" and op in dr.NOT_ATTEMPTED, op


def test_deferral_tables_name_only_family_tree_or_memories_writes():
    writes = _writes()
    ops, _ex, _areas, registry = load_inputs()
    writes.update({o.operation_id: "x" for o in ops
                   if o.kind != "read" and o.area in (dr.P5_AREA, dr.P7_AREA)})
    for op in (*dr.NOT_ATTEMPTED, *dr.WRITE_DEFERRED):
        assert op in writes, op
    assert set(dr.WRITE_DEFERRED) <= set(dr.WRITE_DEFERRED_TIERS)
    claimed = {op for recipe in dr.RECIPES.values() for op in recipe.ops}
    assert not set(dr.NOT_ATTEMPTED) & claimed  # a claimed write is not "not attempted"


def test_an_unaccounted_write_is_an_error(monkeypatch):
    monkeypatch.setattr(dr, "NOT_ATTEMPTED", {})
    with pytest.raises(SystemExit, match="no recorded deferral reason"):
        dr.build_p3_report()
