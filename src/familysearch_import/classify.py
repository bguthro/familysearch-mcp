"""Living status and document provenance classification."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from .embedded import IMAGE_EXTS, embedded_urls
from .scan import Person, nfc

DEFAULT_BANNED: tuple[str, ...] = (
    "fold3",
    "ancestry",
    "newspapers.com",
    "myheritage",
    "findagrave",
    "find a grave",
)

DOCUMENT_EXTS = IMAGE_EXTS | {".pdf"}

_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("obituary", ("obituary", "obit")),
    ("census", ("census",)),
    ("birth", ("birth",)),
    ("marriage", ("marriage", "marr")),
    ("death", ("death",)),
    ("baptism", ("baptism", "baptismal", "christening")),
    ("manifest", ("manifest",)),
    ("naturalization", ("naturalization", "naturalisation")),
    ("will/probate", ("will", "probate")),
    ("photo", ("photo", "portrait")),
)


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", nfc(s).lower())


def living_status(
    person: Person, record_status: str | None = None
) -> Literal["living", "deceased", "unknown"]:
    if record_status in ("deceased", "living"):
        return record_status  # type: ignore[return-value]
    if person.death is not None:
        return "deceased"
    if person.birth is not None and person.birth >= 1900:
        return "living"
    return "unknown"


@dataclass(frozen=True)
class DocumentVerdict:
    path: Path
    kind: str
    provider: str | None
    source_url: str | None
    action: Literal["include", "cite-only", "skip"]
    reason: str


def document_kind(name: str) -> str:
    low = nfc(name).lower()
    for kind, words in _KINDS:
        if any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            return kind
    return "other"


def _banned_match(banned: Sequence[str], text: str) -> str | None:
    sq = _squash(text)
    for b in banned:
        if _squash(b) and _squash(b) in sq:
            return b
    return None


def classify_document(
    person: Person,
    path: Path,
    status: str,
    banned: Sequence[str] = DEFAULT_BANNED,
) -> DocumentVerdict:
    path = Path(path)
    name = path.name
    kind = document_kind(name)

    def verdict(action, reason, provider=None, url=None):
        return DocumentVerdict(path, kind, provider, url, action, reason)

    if status != "deceased":
        return verdict("skip", "living" if status == "living" else "unknown status")
    low = nfc(name).lower()
    if low.startswith("citation") or low.startswith("source information"):
        return verdict("skip", "citation sheet")
    if path.suffix.lower() not in DOCUMENT_EXTS:
        return verdict("skip", "not a document file")

    urls = embedded_urls(path)
    provider = _banned_match(banned, name)
    if provider is None:
        for u in urls:
            host = urlparse(u).hostname or ""
            provider = _banned_match(banned, host)
            if provider:
                break
    if provider is not None:
        if urls:
            return verdict("cite-only", "banned provider, cite as text", provider, urls[0])
        return verdict("skip", "banned provider, no original source", provider)
    return verdict("include", "ok", None, urls[0] if urls else None)
