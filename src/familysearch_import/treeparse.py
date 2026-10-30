"""Parse the tree diagram in a line's FamilyTree.md into people and relationships. Pure.

The parser never guesses: a line it cannot parse is reported in `unparsed_lines`, and so is
everything nested under it. A child written directly under a person with several marriages
gets only that person as a parent (note "ambiguous"), never a spouse chosen by position.
Annotation lines (not people) are attached as notes to the node they sit under.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Node:
    line: str
    index: int
    text: str
    name: str
    birth: int | None
    death: int | None
    approx: bool
    deceased: bool = False  # death year, d. / "the late" / "deceased" marker
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Edge:
    kind: Literal["parent-child", "spouse"]
    a: int  # parent / first spouse
    b: int  # child / second spouse
    order: int | None  # marriage order from m1./m2.
    note: str


@dataclass(frozen=True)
class ParsedTree:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    unparsed_lines: tuple[str, ...]


_Y = r"(?:\d{3,4}(?:/\d{1,4})?|\?)"
_PREF = r"(?:~|c\.|ca\.?|abt\.?|about|aft\.?|after|bef\.?|before)"
_DASH = "[–—-]"
_RANGE = re.compile(
    rf"^(?:(?P<k>[bd])\.\s*)?(?P<p1>{_PREF}\s*)?(?P<y1>{_Y})?"
    rf"(?:\s*(?P<dash>{_DASH})\s*(?P<p2>{_PREF}\s*)?(?P<y2>{_Y})?)?$"
)
_DEATH_ANY = re.compile(rf"\bd\.\s*(?:{_PREF}\s*)?(\d{{3,4}})")
_DECEASED_TEXT = re.compile(r"\b(?:the late|deceased)\b|\bd\.\s*\d{3,4}", re.I)
_HEADING = re.compile(r"^\s{0,3}#+\s+(.*)$")
_CONN = re.compile(r"^[\s│|]*([├└])[─-]*\s*(.*)$")
_MARRIAGE = re.compile(r"\s+m(\d*)\.\s+")
_MLINE = re.compile(r"^m(\d*)\.\s+(.*)$")
_MDATE_COMMA = re.compile(rf"^({_PREF}?\s*{_Y})\s*,\s*(.+)$")
_MDATE_SPACE = re.compile(rf"^({_PREF}?\s*\d{{3,4}}(?:/\d{{1,4}})?)\s+(?=[^\W\d_])(.+)$")
_PAREN = re.compile(r"\(([^()]*)\)")
_NAME_DASH = re.compile(r"\s*—\s*|\s+–\s+|\s+-\s+")


def _year(s: str | None) -> int | None:
    m = re.match(r"\d{3,4}", s or "")
    return int(m.group()) if m else None


def _dates(content: str):
    """(birth, death, approx, deceased) when the group is a date / death group, else None."""
    c = content.strip()
    first = re.split(r"[,;]", c, maxsplit=1)[0].strip()
    m = _RANGE.match(first)
    if m and (m.group("y1") or m.group("y2")):
        approx = bool(m.group("p1") or m.group("p2"))
        y1, y2 = _year(m.group("y1")), _year(m.group("y2"))
        if m.group("dash"):
            return y1, y2, approx, y2 is not None
        if m.group("k") == "b":
            return y1, None, approx, False
        if m.group("k") == "d":
            return None, y1, approx, y1 is not None
        return None, None, approx, False  # a lone year with no role: not guessed
    d = _DEATH_ANY.search(c)
    if d:
        return None, int(d.group(1)), bool(re.search(_PREF, c[: d.start(1)])), True
    if re.search(r"\b(?:the late|deceased)\b", c, re.I):
        return None, None, False, True
    return None


def _parse_person(text: str, last_comma: bool = False):
    """(name, birth, death, approx, deceased, notes) or None when unparseable."""
    text = text.strip()
    found = None
    for m in _PAREN.finditer(text):
        d = _dates(m.group(1))
        if d is not None:
            found = (m, d)  # the last date-like group wins
    birth = death = None
    approx = deceased = False
    if found:
        m, (birth, death, approx, deceased) = found
        text = text[: m.start()] + " " + text[m.end():]
    notes: list[str] = []
    parts = _NAME_DASH.split(text, 1)
    if len(parts) == 2 and parts[1].strip():
        notes.append(" ".join(parts[1].split()))
    name = " ".join(parts[0].split())
    if last_comma and "," in name:
        name = name.rsplit(",", 1)[1].strip()
    if not re.search(r"[^\W\d_]", name) or name.count("(") != name.count(")") or name.count('"') % 2:
        return None
    deceased = deceased or death is not None or bool(_DECEASED_TEXT.search(text))
    return name, birth, death, approx, deceased, tuple(notes)


def _is_note(content: str) -> bool:
    c = content.strip()
    if c.startswith("(") and c.count("(") == c.count(")"):
        return True
    return bool(c) and c[0].islower() and not any(_dates(g) is not None for g in _PAREN.findall(c))


def _fenced_blocks(text: str) -> list[tuple[str, list[str]]]:
    """(preceding heading text, lines) for every fenced block."""
    out: list[tuple[str, list[str]]] = []
    heading = ""
    cur: list[str] | None = None
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            if cur is None:
                cur = []
            else:
                out.append((heading, cur))
                cur = None
            continue
        if cur is not None:
            cur.append(raw.rstrip())
            continue
        h = _HEADING.match(raw)
        if h:
            heading = h.group(1)
    return out


def _diagram_lines(text: str) -> list[str]:
    blocks = _fenced_blocks(text)
    for heading, lines in blocks:
        if "tree diagram" in heading.lower():
            return lines
    for _, lines in blocks:
        if any("└─" in ln or "├─" in ln for ln in lines):
            return lines
    return []


@dataclass
class _Entry:
    col: int
    kind: str  # person | mline | bad | note
    node: int = -1
    owner: "_Entry | None" = None
    spouses: list = None  # type: ignore[assignment]  (inline + m-line spouse node indexes)

    def __post_init__(self):
        self.spouses = []


def parse_family_tree(line: str, text: str) -> ParsedTree:
    recs: list[dict] = []
    edges: list[Edge] = []
    unparsed: list[str] = []
    children: list[tuple[int, _Entry]] = []  # (child node index, parent entry)
    stack: list[_Entry] = []
    last_node = -1

    def add(raw: str, parsed) -> int:
        name, birth, death, approx, deceased, notes = parsed
        recs.append(dict(text=raw.strip(), name=name, birth=birth, death=death, approx=approx,
                         deceased=deceased, notes=list(notes)))
        return len(recs) - 1

    def marriage(seg: str):
        s = seg.strip()
        m = _MDATE_COMMA.match(s) or _MDATE_SPACE.match(s)
        return (m.group(1).strip(), m.group(2)) if m else ("", s)

    for raw in _diagram_lines(text):
        if not raw.strip() or not raw.strip("│| \t"):
            continue
        c = _CONN.match(raw)
        if c:
            col, content = c.start(1), c.group(2).strip()
        elif raw[0] not in " \t":
            col, content = -1, raw.strip()
        else:
            unparsed.append(raw)
            continue
        while stack and stack[-1].col >= col:
            stack.pop()
        parent = stack[-1] if stack else None

        if parent is not None and parent.kind in ("bad", "note"):
            unparsed.append(raw)
            stack.append(_Entry(col, "bad"))
            continue

        ml = _MLINE.match(content)
        if ml:
            date, rest = marriage(ml.group(2))
            p = _parse_person(rest, last_comma=True)
            if parent is None or parent.kind != "person" or p is None:
                unparsed.append(raw)
                stack.append(_Entry(col, "bad"))
                continue
            e = _Entry(col, "mline", add(rest, p), parent)
            last_node = e.node
            parent.spouses.append(e.node)
            edges.append(Edge("spouse", parent.node, e.node, int(ml.group(1)) if ml.group(1) else None,
                              f"m. {date}".strip()))
            stack.append(e)
            continue

        if _is_note(content):
            target = parent.node if parent is not None else last_node
            if target < 0:
                unparsed.append(raw)
                stack.append(_Entry(col, "bad"))
            else:
                recs[target]["notes"].append(content)
                stack.append(_Entry(col, "note"))
            continue

        parts = _MARRIAGE.split(content)  # [person, digit, seg, digit, seg, ...]
        person = _parse_person(parts[0])
        spouse_parsed = []
        for i in range(1, len(parts), 2):
            date, rest = marriage(parts[i + 1])
            spouse_parsed.append((parts[i], date, rest, _parse_person(rest, last_comma=True)))
        if person is None or any(s[3] is None for s in spouse_parsed):
            unparsed.append(raw)
            stack.append(_Entry(col, "bad"))
            continue
        e = _Entry(col, "person", add(parts[0], person))
        last_node = e.node
        for n, (digit, date, rest, sp) in enumerate(spouse_parsed, start=1):
            s = add(rest, sp)
            e.spouses.append(s)
            order = int(digit) if digit else (n if len(spouse_parsed) > 1 else None)
            edges.append(Edge("spouse", e.node, s, order, f"m. {date}".strip()))
        if parent is not None:
            children.append((e.node, parent))
        stack.append(e)

    for child, parent in children:
        if parent.kind == "mline":
            edges.append(Edge("parent-child", parent.owner.node, child, None, ""))
            edges.append(Edge("parent-child", parent.node, child, None, ""))
        elif len(parent.spouses) == 1:
            edges.append(Edge("parent-child", parent.node, child, None, ""))
            edges.append(Edge("parent-child", parent.spouses[0], child, None, ""))
        else:
            note = "other parent ambiguous (several marriages)" if parent.spouses else ""
            edges.append(Edge("parent-child", parent.node, child, None, note))
    nodes = tuple(Node(line, i, r["text"], r["name"], r["birth"], r["death"], r["approx"], r["deceased"],
                       tuple(r["notes"])) for i, r in enumerate(recs))
    return ParsedTree(nodes, tuple(edges), tuple(unparsed))
