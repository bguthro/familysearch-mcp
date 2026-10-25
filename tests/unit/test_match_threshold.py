import pytest

from test_mcp_tools_write import OLD, build, hit, writer_calls


def run(hits, **kw):
    tools, svc, _ = build(hits)
    return tools["create_person"](**OLD, **kw), svc


def test_all_weak_hits_proceed_and_are_counted():
    out, svc = run([hit(2, "A"), hit(2, "B"), hit(2, "C")])
    assert out["status"] == "created"
    assert out["weak_candidates_ignored"] == 3
    assert len(svc.writer.create_person.calls) == 1


def test_empty_list_proceeds_with_zero_ignored():
    out, _ = run([])
    assert out["status"] == "created" and out["weak_candidates_ignored"] == 0


def test_one_strong_among_weak_lists_only_strong():
    out, svc = run([hit(2, "A"), hit(3, "B"), hit(2, "C")])
    assert out["status"] == "matches_found"
    assert [c["id"] for c in out["candidates"]] == ["B"]
    assert writer_calls(svc) == 0


@pytest.mark.parametrize("conf,blocks", [(2, False), (3, True), (4, True), (1, False), (0, False)])
def test_boundary(conf, blocks):
    out, svc = run([hit(conf, "A")])
    assert (out["status"] == "matches_found") is blocks
    assert (writer_calls(svc) == 0) is blocks


@pytest.mark.parametrize("conf", [None, "x", True])
def test_unknown_confidence_blocks(conf):
    out, svc = run([hit(conf, "A"), hit(2, "B")])
    assert out["status"] == "matches_found"
    assert [c["id"] for c in out["candidates"]] == ["A"]
    assert out["candidates"][0]["confidence"] is None
    assert writer_calls(svc) == 0


def test_none_sorts_first_then_descending():
    out, _ = run([hit(3, "A"), hit(None, "B"), hit(4, "C")])
    assert [c["id"] for c in out["candidates"]] == ["B", "C", "A"]


def test_more_than_ten_qualifying_capped_sorted():
    out, _ = run([hit(3 + (i % 3), f"P{i}") for i in range(15)] + [hit(2, "W")])
    assert len(out["candidates"]) == 10
    confs = [c["confidence"] for c in out["candidates"]]
    assert confs == sorted(confs, reverse=True) and confs[0] == 5


def test_message_states_threshold():
    out, _ = run([hit(4, "A")])
    assert "confidence >= 3" in out["message"]
    assert "Ben" in out["message"] and "create_despite_matches" in out["message"]


def test_override_skips_search_zero_ignored():
    out, svc = run([hit(5)], create_despite_matches=True)
    assert out["status"] == "created" and out["weak_candidates_ignored"] == 0
    assert svc.search.tree_search.calls == []
