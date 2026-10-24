"""Pure safety guards for write tools: aggregator provenance and living/deceased.

Standard library only; no I/O.
"""

import re

# Canonical aggregator name -> lowercase, separator-free needles.
AGGREGATOR_PATTERNS: dict[str, tuple[str, ...]] = {
    "fold3": ("fold3",),
    "ancestry": ("ancestry",),
    "newspapers.com": ("newspaperscom",),
    "myheritage": ("myheritage",),
    "findagrave": ("findagrave",),
}

_SEPARATORS = re.compile(r"[\s_.\-]+")
_YEAR = re.compile(r"(?<!\d)\d{4}(?!\d)")
_MAX_AGE = 110


def _match(text: object) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    stripped = _SEPARATORS.sub("", text.lower())
    for name, needles in AGGREGATOR_PATTERNS.items():
        if any(n in stripped for n in needles):
            return name
    return None


def provenance_blocked(filename: str, provenance: str | None) -> str | None:
    """Return the canonical aggregator name indicated by filename or provenance.

    Filename is checked first. Known, accepted false positive: a family-made
    file such as "Anderson Ancestry Chart.pdf" is blocked. The caller can
    override per file, and a missed Fold3/Ancestry image (rights/takedown
    risk) costs more than an extra refusal.
    """
    return _match(filename) or _match(provenance)


def _birth_year(birth_date: object) -> int | None:
    if not isinstance(birth_date, str):
        return None
    m = _YEAR.search(birth_date)
    return int(m.group()) if m else None


def classify_life_status(
    birth_date: str | None,
    death_date: str | None,
    *,
    today_year: int,
    asserted_living: bool = False,
) -> str:
    """Classify as deceased, living, possibly_living or deceased_by_age.

    Fails safe: anything uncertain is "possibly_living".
    """
    if isinstance(death_date, str) and death_date.strip():
        return "possibly_living" if asserted_living else "deceased"
    if asserted_living:
        return "living"
    year = _birth_year(birth_date)
    if year is None:
        return "possibly_living"
    if today_year - year > _MAX_AGE:
        return "deceased_by_age"
    return "possibly_living"
