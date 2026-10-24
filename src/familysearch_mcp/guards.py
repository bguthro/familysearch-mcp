"""Pure safety guards for write tools: aggregator provenance and living/deceased.

Standard library only; no I/O.
"""

import re
import unicodedata

# Canonical aggregator name -> lowercase, separator-free needles.
AGGREGATOR_PATTERNS: dict[str, tuple[str, ...]] = {
    "fold3": ("fold3",),
    "ancestry": ("ancestry",),
    "newspapers.com": ("newspaperscom",),
    "myheritage": ("myheritage",),
    "findagrave": ("findagrave",),
}

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_WORD = re.compile(r"[^\W\d_]+")
_YEAR = re.compile(r"(?<!\d)\d{4}(?!\d)")
_MAX_AGE = 110


def _match(text: object) -> str | None:
    # Residuals, accepted: cross-script homoglyphs (e.g. Cyrillic "a") are NOT
    # handled; cross-word over-refusal ("Dan Cestry") is accepted because the
    # caller can override per file.
    if not isinstance(text, str) or not text:
        return None
    norm = _NON_ALNUM.sub("", unicodedata.normalize("NFKC", text).casefold())
    for name, needles in AGGREGATOR_PATTERNS.items():
        if any(n in norm for n in needles):
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


_DATE_WORDS = frozenset(
    "jan january feb february mar march apr april may jun june jul july aug "
    "august sep sept september oct october nov november dec december "
    "abt about ca circa c cal calc est estimated bef before aft after "
    "between and to from of or in st nd rd th".split()
)


def _extract_year(text: object, today_year: int) -> int | None:
    """Latest plausible year in a date string, or None (unknown).

    Any word outside the English date vocabulary makes the date unknown, so
    e.g. "Lot 1234", "1500 hrs 3 June 1979" and "3 Juni 1879" fail closed.
    Taking the max year is monotone-safe: junk can only make a person look
    younger, never older.
    """
    if not isinstance(text, str):
        return None
    if any(w.lower() not in _DATE_WORDS for w in _WORD.findall(text)):
        return None
    years = [int(y) for y in _YEAR.findall(text)]
    years = [y for y in years if 1000 <= y <= today_year + 1]
    return max(years) if years else None


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
    death_year = _extract_year(death_date, today_year)
    if death_year is not None and death_year <= today_year:
        return "possibly_living" if asserted_living else "deceased"
    if asserted_living:
        return "living"
    year = _extract_year(birth_date, today_year)
    if year is None:
        return "possibly_living"
    if today_year - year > _MAX_AGE:
        return "deceased_by_age"
    return "possibly_living"
