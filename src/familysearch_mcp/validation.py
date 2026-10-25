"""Boundary validation helpers for MCP tool arguments."""

from __future__ import annotations

import re
from typing import Any

_PERSON_ID = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{3,5}")


def validate_person_id(value: Any) -> str:
    """Return the normalized (stripped, upper-case) person id or raise ValueError."""
    if isinstance(value, str):
        stripped = value.strip()
        # ASCII-only BEFORE upper-casing: str.upper() maps lookalikes such as
        # dotless i or long s onto ASCII letters, silently changing the id.
        if stripped.isascii():
            candidate = stripped.upper()
            if _PERSON_ID.fullmatch(candidate):
                return candidate
    raise ValueError(
        f"invalid person id {value!r}: expected a FamilySearch person id like 'GKLV-DY8'"
    )


def validate_source_id(value: Any) -> str:
    """Return the normalized source description id (e.g. '71FD-SL2') or raise ValueError."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isascii():
            candidate = stripped.upper()
            if _PERSON_ID.fullmatch(candidate):
                return candidate
    raise ValueError(
        f"invalid source description id {value!r}: expected an id like '71FD-SL2'"
    )


def validate_memory_id(value: Any) -> str:
    """Return a memory id as 1-20 ASCII digits (no leading zero) or raise ValueError."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isascii() and re.fullmatch(r"[1-9][0-9]{0,19}", stripped):
            return stripped
    raise ValueError(
        f"invalid memory id {value!r}: expected 1-20 ASCII digits like '123456789'"
    )


def clamp(value: Any, lo: int, hi: int) -> int:
    """Coerce value to int and bound it to [lo, hi]; non-integers become lo."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return lo
    return max(lo, min(hi, n))
