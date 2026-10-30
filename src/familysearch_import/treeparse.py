"""Parse the tree diagram in a line's FamilyTree.md into people and relationships. Pure.

The parser never guesses: a line it cannot parse is reported in `unparsed_lines`, and so is
everything nested under it. A child written directly under a person with several marriages
gets only that person as a parent (note "ambiguous"), never a spouse chosen by position.
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


_Y = r"(?:\d{3,4}|\?)"
_APPROX = r"(?:~|c\.\s*)"
_RANGE = re.compile(rf"^({_APPROX}?\s*{_Y})?\s*[–-]\s*({_APPROX}?\s*{_Y})?$")
_SINGLE = re.compile(rf"^([bd])\.\s*{_APPROX}?\s*({_Y})$")
_HEADING = re.compile(r"^\s{0,3}#+\s+(.*)$")
_CONN = re.compile(r"^[\s│|]*([├└])[─-]*\s*(.*)$")
_MARRIAGE = re.compile(r"\s+m(\d*)\.\s+")
_MLINE = re.compile(r"^m(\d*)\.\s+(.*)$")
_MDATE = re.compile(rf"^({_APPROX}?\s*(?:\d{{3,4}}|\?))\s*,\s*(.+)$")
_PAREN = re.compile(r"\(([^()]*)\)")
_DIGITS = re.compile(r"\d")


def _year(s: str | None) -> int | None:
    s = (s or "").strip().lstrip("~").removeprefix("c.").strip()
    return int(s) if s.isdigit() else None


def _dates(content: str):
    """(birth, death, approx) for a date group, None when it is not a date group,
    "bad" when it looks like dates but is not understood."""
    body = content.split(",", 1)[0].strip()
    approx = "~" in body or "c." in body
    m = _SINGLE.match(body)
    if m:
        y = _year(m.group(2))
        return (y, None, approx) if m.group(1) == "b" else (None, y, approx)
    m = _RANGE.match(body)
    if m and (m.group(1) or m.group(2)):
        return _year(m.group(1)), _year(m.group(2)), approx
    if _DIGITS.search(body):
        return "bad"
    return None


def _parse_person(text: str):
    """(name, birth, death, approx) or None when unparseable."""
    text = text.strip()
    found = None
    for m in _PAREN.finditer(text):
        d = _dates(m.group(1))
        if d == "bad":
            return None
        if d is not None:
            found = (m, d)  # the last date-like group wins
    birth = death = None
    approx = False
    if found:
        m, (birth, death, approx) = found
        text = text[: m.start()] + " " + text[m.end():]
    name = " ".join(text.split())
    if not re.search(r"[^\W\d_]", name) or name.count("(") != name.count(")") or name.count('"') % 2:
        return None
    return name, birth, death, approx


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
    kind: str  # person | mline | bad
    node: int = -1
    owner: "_Entry | None" = None
    spouses: list = None  # type: ignore[assignment]  (inline + m-line spouse node indexes)

    def __post_init__(self):
        self.spouses = []


def parse_family_tree(line: str, text: str) -> ParsedTree:
    nodes: list[Node] = []
    edges: list[Edge] = []
    unparsed: list[str] = []
    children: list[tuple[int, _Entry]] = []  # (child node index, parent entry)
    stack: list[_Entry] = []

    def add(raw: str, parsed) -> int:
        name, birth, death, approx = parsed
        nodes.append(Node(line, len(nodes), raw.strip(), name, birth, death, approx))
        return len(nodes) - 1

    def marriage(seg: str):
        m = _MDATE.match(seg.strip())
        return (m.group(1).strip(), m.group(2)) if m else ("", seg)

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

        if parent is not None and parent.kind == "bad":
            unparsed.append(raw)
            stack.append(_Entry(col, "bad"))
            continue

        ml = _MLINE.match(content)
        if ml:
            p = _parse_person(marriage(ml.group(2))[1])
            if parent is None or parent.kind != "person" or p is None:
                unparsed.append(raw)
                stack.append(_Entry(col, "bad"))
                continue
            date, rest = marriage(ml.group(2))
            e = _Entry(col, "mline", add(rest, _parse_person(rest)), parent)
            parent.spouses.append(e.node)
            edges.append(Edge("spouse", parent.node, e.node, int(ml.group(1)) if ml.group(1) else None,
                              f"m. {date}".strip()))
            stack.append(e)
            continue

        parts = _MARRIAGE.split(content)  # [person, digit, seg, digit, seg, ...]
        person = _parse_person(parts[0])
        spouse_parsed = []
        for i in range(1, len(parts), 2):
            date, rest = marriage(parts[i + 1])
            spouse_parsed.append((parts[i], date, rest, _parse_person(rest)))
        if person is None or any(s[3] is None for s in spouse_parsed):
            unparsed.append(raw)
            stack.append(_Entry(col, "bad"))
            continue
        e = _Entry(col, "person", add(parts[0], person))
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
    return ParsedTree(tuple(nodes), tuple(edges), tuple(unparsed))
