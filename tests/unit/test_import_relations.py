import json
from pathlib import Path

import pytest

from familysearch_import.relations import RelationsError, load_relations, to_parsed_tree
from familysearch_import.relgap import evaluate_edges, map_nodes
from familysearch_import.scan import Person

FOLDERS = {"Line A/Adam Example", "Line A/Cal Example"}


def node(id, name="Some Name", **kw):
    d = {"id": id, "name": name, "birth": None, "death": None, "status": "unknown", "folder": None,
         "source": "text"}
    d.update(kw)
    return d


def doc(nodes=None, edges=None, **kw):
    d = {"version": 1, "line": "Line A", "reviewed": True,
         "nodes": nodes if nodes is not None else [
             node("n1", "Adam Example", birth=1800, death=1860, status="deceased", folder="Line A/Adam Example"),
             node("n2", "Cal Example", birth=1825, death=1890, status="deceased")],
         "edges": edges if edges is not None else [
             {"kind": "parent-child", "a": "n1", "b": "n2", "order": None, "marriage_year": None}]}
    d.update(kw)
    return d


def load(tmp_path, d):
    p = tmp_path / "Line A.json"
    p.write_text(json.dumps(d))
    return load_relations(p, FOLDERS)


def errors(tmp_path, d):
    with pytest.raises(RelationsError) as ei:
        load(tmp_path, d)
    return ei.value.errors


def test_valid_file_loads(tmp_path):
    rel = load(tmp_path, doc())
    assert rel.line == "Line A" and rel.reviewed and len(rel.nodes) == 2 and len(rel.edges) == 1
    assert rel.nodes[0].folder == "Line A/Adam Example"


def test_bad_json_and_top_level(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{nope")
    with pytest.raises(RelationsError) as ei:
        load_relations(p, FOLDERS)
    assert "x.json" in ei.value.errors[0]
    p.write_text("[]")
    with pytest.raises(RelationsError):
        load_relations(p, FOLDERS)


def test_header_rules(tmp_path):
    assert any("version" in e for e in errors(tmp_path, doc(version=2)))
    assert any("reviewed" in e for e in errors(tmp_path, doc(reviewed="yes")))
    assert any("line" in e for e in errors(tmp_path, doc(line="")))


def test_node_rules_name_file_and_index(tmp_path):
    errs = errors(tmp_path, doc(nodes=[node("a"), node("a"), node("b", name=" "), node("c", status="dead"),
                                       node("d", folder="Line A/Nobody"), node("e", birth="1800"),
                                       node("f", source=None)], edges=[]))
    text = "\n".join(errs)
    assert all(e.startswith("Line A.json: nodes[") for e in errs)
    for needle in ("nodes[1]: duplicate id", "nodes[2]: name", "nodes[3]: status", "nodes[4]: folder",
                   "nodes[5]: birth", "nodes[6]: source"):
        assert needle in text


def test_booleans_are_not_integers(tmp_path):
    assert any("birth" in e for e in errors(tmp_path, doc(nodes=[node("a", birth=True)], edges=[])))


def test_deceased_needs_death_year_or_note(tmp_path):
    assert any("deceased needs" in e for e in errors(tmp_path, doc(nodes=[node("a", status="deceased")], edges=[])))
    ok = doc(nodes=[node("a", status="deceased", deceased_note="the late X")], edges=[])
    assert load(tmp_path, ok).nodes[0].deceased_note == "the late X"


def test_modern_birth_cannot_be_deceased_without_note(tmp_path):
    errs = errors(tmp_path, doc(nodes=[node("a", birth=1950, status="deceased")], edges=[]))
    assert any("1900" in e for e in errs)
    ok = doc(nodes=[node("a", birth=1950, status="deceased", deceased_note="obituary")], edges=[])
    assert load(tmp_path, ok).nodes[0].status == "deceased"
    ok2 = doc(nodes=[node("a", birth=1950, death=2020, status="deceased")], edges=[])
    assert load(tmp_path, ok2)


def test_edge_rules(tmp_path):
    bad = [{"kind": "friend", "a": "n1", "b": "n2", "order": None, "marriage_year": None},
           {"kind": "spouse", "a": "n1", "b": "zz", "order": None, "marriage_year": None},
           {"kind": "spouse", "a": "n1", "b": "n1", "order": None, "marriage_year": None},
           {"kind": "spouse", "a": "n1", "b": "n2", "order": "1", "marriage_year": None}]
    text = "\n".join(errors(tmp_path, doc(edges=bad)))
    for needle in ("edges[0]: kind", "edges[1]: b does not reference", "edges[2]: a and b must differ",
                   "edges[3]: order"):
        assert needle in text


def test_adapter_preserves_semantics_and_edges(tmp_path):
    d = doc(nodes=[node("n1", "Adam Example", birth=1800, death=1860, status="deceased", folder="Line A/Adam Example"),
                   node("n2", "Mystery", status="unknown"), node("n3", "Kay Young", birth=2005, status="living"),
                   node("n4", "Old Soul", status="deceased", deceased_note="the late Old Soul")],
            edges=[{"kind": "spouse", "a": "n1", "b": "n2", "order": 2, "marriage_year": 1822},
                   {"kind": "parent-child", "a": "n1", "b": "n3", "order": None, "marriage_year": None}])
    t = to_parsed_tree(load(tmp_path, d))
    assert t.unparsed_lines == () and [n.index for n in t.nodes] == [0, 1, 2, 3]
    assert t.nodes[0].folder == "Line A/Adam Example" and t.nodes[0].deceased and t.nodes[0].line == "Line A"
    assert t.nodes[3].deceased and t.nodes[1].status == "unknown" and not t.nodes[1].deceased
    e0, e1 = t.edges
    assert (e0.kind, e0.a, e0.b, e0.order, e0.note) == ("spouse", 0, 1, 2, "m. 1822")
    assert (e1.kind, e1.a, e1.b, e1.note) == ("parent-child", 0, 2, "")


def test_map_nodes_uses_explicit_folder_and_status(tmp_path):
    adam = Person("Line A", "Adam Example", Path("/nonexistent/a"), 1800, 1860, "Adam Example", (), (), frozenset())
    other = Person("Line A", "Adam Example Jr", Path("/nonexistent/b"), 1801, 1861, "Adam Example", (), (), frozenset())
    d = doc(nodes=[node("n1", "A. Example, the elder", folder="Line A/Adam Example", status="deceased",
                        death=1860),
                   node("n2", "Mystery", status="unknown"), node("n3", "Kay Young", status="living"),
                   node("n4", "Old Soul", status="deceased", deceased_note="the late")], edges=[])
    d["nodes"][0]["folder"] = "Line A/Adam Example"
    p = tmp_path / "Line A.json"
    p.write_text(json.dumps(d))
    rel = load_relations(p, {"Line A/Adam Example", "Line A/Adam Example Jr"})
    m = map_nodes({"Line A": to_parsed_tree(rel)}, [adam, other])
    assert m[("Line A", 0)].person is adam and m[("Line A", 0)].status == "mapped"  # name would be ambiguous
    assert m[("Line A", 1)].unknown and m[("Line A", 1)].living
    assert m[("Line A", 2)].living and not m[("Line A", 2)].unknown
    assert not m[("Line A", 3)].living


def test_evaluate_edges_runs_on_an_adapted_tree(tmp_path):
    adam = Person("Line A", "Adam Example", Path("/nonexistent/a"), 1800, 1860, "Adam Example", (), (), frozenset())
    cal = Person("Line A", "Cal Example", Path("/nonexistent/c"), 1825, 1890, "Cal Example", (), (), frozenset())
    rel = load(tmp_path, doc(nodes=[
        node("n1", "Adam Example", birth=1800, death=1860, status="deceased", folder="Line A/Adam Example"),
        node("n2", "Cal Example", birth=1825, death=1890, status="deceased", folder="Line A/Cal Example")]))
    links = {"Line A/Adam Example": "AAAA-1", "Line A/Cal Example": "CCCC-1"}

    class R:
        def get_relatives(self, i):
            return []

    res = evaluate_edges({"Line A": to_parsed_tree(rel)}, [adam, cal], links, {}, R())
    assert [e.status for e in res.edges] == ["missing-in-familysearch"]
