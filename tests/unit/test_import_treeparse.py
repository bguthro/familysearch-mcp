from familysearch_import.treeparse import parse_family_tree

CHAIN = """# Line A

## Direct line

1. Ann Example
2. Bob Sample

## Tree diagram

```
Adam Example (b.1800, Ohio) m. 1822, Eve Sample (1802–1880)
 └─ Cal Example (1825–1890) m. 1850, Dee Other (1828–1900)
       └─ Eli Example (b.1855) m. Fay Third (b.1857)
          │  ├─ Gus Example (b.2010)
          │  └─ Hal Example (b.2012)
```

## Notes
```
not a diagram
```
"""


def by_name(tree):
    return {n.name: n for n in tree.nodes}


def edges(tree, kind):
    return {(tree.nodes[e.a].name, tree.nodes[e.b].name) for e in tree.edges if e.kind == kind}


def test_chain_marriage_inline_and_children():
    t = parse_family_tree("Line A", CHAIN)
    assert t.unparsed_lines == ()
    n = by_name(t)
    assert n["Adam Example"].birth == 1800 and n["Adam Example"].death is None
    assert n["Eve Sample"].birth == 1802 and n["Eve Sample"].death == 1880
    assert n["Adam Example"].line == "Line A"
    assert [x.index for x in t.nodes] == list(range(len(t.nodes)))
    assert edges(t, "spouse") == {("Adam Example", "Eve Sample"), ("Cal Example", "Dee Other"),
                                  ("Eli Example", "Fay Third")}
    pc = edges(t, "parent-child")
    assert ("Adam Example", "Cal Example") in pc and ("Eve Sample", "Cal Example") in pc
    assert ("Cal Example", "Eli Example") in pc and ("Dee Other", "Eli Example") in pc
    # continuation lines with the pipe prefix still attach to Eli and Fay
    assert ("Eli Example", "Gus Example") in pc and ("Fay Third", "Hal Example") in pc
    sp = [e for e in t.edges if e.kind == "spouse"][0]
    assert "1822" in sp.note


M1M2 = """## Tree diagram
```
Adam Example (1800–1860) m. 1822, Eve Sample (1802–1880)
 └─ Cal Example (1825–1890) m. 1850, Dee Other (1828–1900)
       ├─ m1. 1945, Una First
       │  └─ Kay Example (b.1946)
       └─ m2. ~1948, Bess "Bessie" Second (1919–1993)
          └─ Wayne Example (b.1952) m. Claudia Zed (b.1951)
```
"""


def test_m1_m2_children_attach_to_the_right_spouse():
    t = parse_family_tree("Line A", M1M2)
    assert t.unparsed_lines == ()
    pc = edges(t, "parent-child")
    assert ("Cal Example", "Kay Example") in pc and ("Una First", "Kay Example") in pc
    assert ("Bess \"Bessie\" Second", "Wayne Example") in pc and ("Cal Example", "Wayne Example") in pc
    assert ("Dee Other", "Wayne Example") not in pc and ("Una First", "Wayne Example") not in pc
    spouses = [e for e in t.edges if e.kind == "spouse" and t.nodes[e.a].name == "Cal Example"]
    assert sorted((e.order, t.nodes[e.b].name) for e in spouses if e.order) == [
        (1, "Una First"), (2, 'Bess "Bessie" Second')]
    bess = by_name(t)['Bess "Bessie" Second']
    assert bess.birth == 1919 and bess.death == 1993
    assert any("~1948" in e.note for e in spouses)


AMBIGUOUS = """## Tree diagram
```
Cal Example (1825–1890) m. 1850, Dee Other (1828–1900)
 ├─ m2. 1880, Una Later
 ├─ Eli Example (1855–1920)
```
"""


def test_child_directly_under_person_with_several_spouses_has_unknown_other_parent():
    t = parse_family_tree("Line A", AMBIGUOUS)
    pc = edges(t, "parent-child")
    assert ("Cal Example", "Eli Example") in pc
    assert ("Dee Other", "Eli Example") not in pc and ("Una Later", "Eli Example") not in pc
    e = [e for e in t.edges if e.kind == "parent-child"][0]
    assert "ambiguous" in e.note


SIBLINGS = """## Tree diagram
```
Adam Example (1800–1860)
 ├─ Cal Example (1825–1890)
 │     └─ Eli Example (1855–1920)
 └─ Dan Example (1827–1900)
```
"""


def test_siblings_and_nested_children_without_marriage():
    t = parse_family_tree("Line A", SIBLINGS)
    pc = edges(t, "parent-child")
    assert pc == {("Adam Example", "Cal Example"), ("Adam Example", "Dan Example"),
                  ("Cal Example", "Eli Example")}


ODD = """## Tree diagram
```
Adam Example (?–1860)
 ├─ Cal Example (~1825–1890)
 ├─ Dan Example (c.1827–?)
 ├─ Mary "Polly" Example (Maria) (b.~1830, Ohio)
 ├─ Nodate Person
 ├─ Ed Example (d.1901)
 ├─ ((( garbled (((
 └─ Fred Example (1840–1900)
```
"""


def test_odd_dates_and_unparsable_line_is_reported():
    t = parse_family_tree("Line A", ODD)
    n = by_name(t)
    assert n["Adam Example"].birth is None and n["Adam Example"].death == 1860
    assert n["Cal Example"].birth == 1825 and n["Cal Example"].approx
    assert n["Dan Example"].birth == 1827 and n["Dan Example"].death is None and n["Dan Example"].approx
    poll = n['Mary "Polly" Example (Maria)']
    assert poll.birth == 1830 and poll.approx and poll.death is None
    nd = n["Nodate Person"]
    assert nd.birth is None and nd.death is None and not nd.approx
    assert n["Ed Example"].death == 1901
    assert n["Fred Example"].birth == 1840
    assert len(t.unparsed_lines) == 1 and "garbled" in t.unparsed_lines[0]
    assert ("Adam Example", "Fred Example") in edges(t, "parent-child")


def test_fallback_block_without_heading_and_empty_input():
    text = "intro\n```\nAdam Example (1800–1860)\n └─ Cal Example (1825–1890)\n```\n"
    t = parse_family_tree("L", text)
    assert edges(t, "parent-child") == {("Adam Example", "Cal Example")}
    empty = parse_family_tree("L", "# nothing here\n")
    assert empty.nodes == () and empty.edges == () and empty.unparsed_lines == ()


def test_m_line_without_owner_is_unparsed():
    t = parse_family_tree("L", "## Tree diagram\n```\nm1. 1900, Orphan Spouse\n```\n")
    assert t.nodes == () and len(t.unparsed_lines) == 1
