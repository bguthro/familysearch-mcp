"""Read-only archive scanner. No network, no personal data."""

from __future__ import annotations

import plistlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

_ID = r"[A-Z0-9]{4}-[A-Z0-9]{3,4}"
_FOLDER_RE = re.compile(r"^(\d{4}|\?)(?:\s*-\s*(\d{4}|\?))?\s+(\S.*)$")
_TREE_PATH_RE = re.compile(rf"^(?:/[a-z]{{2}}(?:-[A-Za-z]+)?)?/tree/person/(?:details/|[a-z]+/)?({_ID})(?:/|$)")
_ANCESTORS_PATH_RE = re.compile(rf"^/[a-z]{{2}}(?:-[A-Za-z]+)?/({_ID})(?:/|$)")
_STRING_RE = re.compile(r"<string>\s*([^<]+?)\s*</string>")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

SKIP_TOP = {"_tools", "_import", "books", "to sort"}
IGNORED_FILES = {"family migration map.html"}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


@dataclass(frozen=True)
class Person:
    line: str
    folder: str
    path: Path
    birth: int | None
    death: int | None
    display_name: str
    files: tuple[Path, ...]
    familysearch_ids: tuple[str, ...]
    book_excluded: frozenset[str]


_ID_TEXT_RE = re.compile(rf"(?<![A-Za-z0-9-])({_ID})(?![A-Za-z0-9])(?!-[A-Za-z0-9])")
_URL_TRAIL = ".,;:)]}>\"'"


@dataclass(frozen=True)
class IdCandidate:
    id: str
    source: str  # file name relative to the person folder
    kind: Literal["webloc", "note", "filename"]


def _plausible_id(s: str) -> bool:
    head, tail = s.split("-")
    if not any(c.isdigit() for c in s):
        return False
    if head.isdigit() and tail.isdigit():
        return False  # numeric pairs / date ranges
    if head.isalpha() and tail.isdigit() and len(tail) == 4:
        return False  # words like TEST-2024
    return True


def _ids_in_text(text: str) -> list[str]:
    found: list[str] = []
    for m in _URL_RE.finditer(text):
        pid = familysearch_person_id(m.group(0).rstrip(_URL_TRAIL))
        if pid and _plausible_id(pid):
            found.append(pid)
    for m in _ID_TEXT_RE.finditer(text):
        if _plausible_id(m.group(1)):
            found.append(m.group(1))
    return found


def mine_candidates(person: Person) -> tuple[IdCandidate, ...]:
    out: list[IdCandidate] = []
    seen: set[IdCandidate] = set()

    def add(c: IdCandidate) -> None:
        if c not in seen:
            seen.add(c)
            out.append(c)

    for f in person.files:
        try:
            rel = str(f.relative_to(person.path))
        except ValueError:
            rel = f.name
        rel = nfc(rel)
        suffix = f.suffix.lower()
        if suffix == ".webloc":
            url = read_webloc_url(f)
            pid = familysearch_person_id(url) if url else None
            if pid:
                add(IdCandidate(pid, rel, "webloc"))
        elif suffix in (".md", ".txt"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for pid in _ids_in_text(text):
                add(IdCandidate(pid, rel, "note"))
        for m in _ID_TEXT_RE.finditer(nfc(f.name)):
            if _plausible_id(m.group(1)):
                add(IdCandidate(m.group(1), rel, "filename"))
    return tuple(out)


def parse_folder_name(name: str) -> tuple[int | None, int | None, str] | None:
    m = _FOLDER_RE.match(nfc(name).strip())
    if not m:
        return None
    b, d, rest = m.groups()
    return (
        int(b) if b != "?" else None,
        int(d) if d and d != "?" else None,
        rest.strip(),
    )


def familysearch_person_id(url: str) -> str | None:
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host == "ancestors.familysearch.org":
        m = _ANCESTORS_PATH_RE.match(u.path)
    elif host == "familysearch.org" or host.endswith(".familysearch.org"):
        m = _TREE_PATH_RE.match(u.path)
    else:
        return None
    return m.group(1) if m else None


def read_webloc_url(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        obj = plistlib.loads(data)
        url = obj.get("URL") if isinstance(obj, dict) else None
        if isinstance(url, str):
            return url
    except Exception:
        pass
    text = data.decode("utf-8", "replace")
    m = _STRING_RE.search(text)
    if m:
        return m.group(1)
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def _ignored_dir(name: str) -> bool:
    return name.startswith(".") or nfc(name).lower() == "questionable"


def _ignored_file(name: str) -> bool:
    n = nfc(name)
    return n.startswith(".") or n.lower() in IGNORED_FILES or n.startswith("Gmail -")


def _isdir(p: Path) -> bool:
    """Directory test that never follows symlinks."""
    try:
        return p.is_dir() and not p.is_symlink()
    except OSError:
        return False


def _listdir(d: Path, problems: list[str] | None, root: Path | None = None) -> list[Path] | None:
    """Sorted children, or None (and a record in problems) when unreadable."""
    try:
        return sorted(d.iterdir())
    except OSError:
        if problems is not None:
            try:
                label = str(d.relative_to(root)) if root else d.name
            except ValueError:
                label = d.name
            problems.append(nfc(label))
        return None


def _collect_files(folder: Path, problems: list[str] | None = None, root: Path | None = None) -> list[Path]:
    out: list[Path] = []
    for child in _listdir(folder, problems, root) or []:
        if _isdir(child):
            if not _ignored_dir(child.name):
                out.extend(_collect_files(child, problems, root))
        elif child.is_file() and not child.is_symlink() and not _ignored_file(child.name):
            out.append(child)
    return out


def _read_book_exclude(files: Sequence[Path]) -> frozenset[str]:
    names: set[str] = set()
    for f in files:
        if f.name == "BookExclude.txt":
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for raw in text.splitlines():
                s = raw.strip()
                if s and not s.startswith("#"):
                    names.add(nfc(s))
    return frozenset(names)


def _line_dirs(root: Path, lines: Sequence[str] | None) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"archive path does not exist: {root}")
    wanted = {nfc(x) for x in lines} if lines is not None else None
    out = []
    for d in sorted(root.iterdir()):
        if not _isdir(d) or d.name.startswith("."):
            continue
        n = nfc(d.name)
        if n.startswith("0000_") or n.lower() in SKIP_TOP or _ignored_dir(n):
            continue
        if wanted is not None and n not in wanted:
            continue
        out.append(d)
    return out


def scan_archive(
    root: Path,
    lines: Sequence[str] | None = None,
    problems: list[str] | None = None,
) -> list[Person]:
    """problems, when given, collects root-relative paths of unreadable folders."""
    root = Path(root)
    people: list[Person] = []
    for line_dir in _line_dirs(root, lines):
        for d in _listdir(line_dir, problems, root) or []:
            if not _isdir(d) or _ignored_dir(d.name):
                continue
            parsed = parse_folder_name(d.name)
            if parsed is None:
                continue
            birth, death, name = parsed
            if _listdir(d, problems, root) is None:
                continue
            files = _collect_files(d, problems, root)
            ids: list[str] = []
            for f in files:
                if f.suffix.lower() == ".webloc":
                    url = read_webloc_url(f)
                    pid = familysearch_person_id(url) if url else None
                    if pid and pid not in ids:
                        ids.append(pid)
            people.append(
                Person(
                    line=nfc(line_dir.name),
                    folder=nfc(d.name),
                    path=d,
                    birth=birth,
                    death=death,
                    display_name=name,
                    files=tuple(files),
                    familysearch_ids=tuple(ids),
                    book_excluded=_read_book_exclude(files),
                )
            )
    return people


def unparsed_folders(root: Path, lines: Sequence[str] | None = None) -> list[tuple[str, str]]:
    """(line, folder) for directories under a line that do not parse as a person."""
    out = []
    for line_dir in _line_dirs(Path(root), lines):
        for d in _listdir(line_dir, None) or []:
            if (
                _isdir(d)
                and not _ignored_dir(d.name)
                and not d.name.startswith("0000_")
                and parse_folder_name(d.name) is None
            ):
                out.append((nfc(line_dir.name), nfc(d.name)))
    return out
