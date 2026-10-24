"""Boundary validation helpers for MCP tool arguments."""

from __future__ import annotations

import re
from typing import Any

_PERSON_ID = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{3,5}")


def validate_person_id(value: Any) -> str:
    """Return the normalized (stripped, upper-case) person id or raise ValueError."""
    if isinstance(value, str):
        candidate = value.strip().upper()
        if _PERSON_ID.fullmatch(candidate):
            return candidate
    raise ValueError(
        f"invalid person id {value!r}: expected a FamilySearch person id like 'GKLV-DY8'"
    )


def clamp(value: Any, lo: int, hi: int) -> int:
    """Coerce value to int and bound it to [lo, hi]; non-integers become lo."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))
