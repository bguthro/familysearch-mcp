import pytest

from familysearch_mcp.validation import clamp, validate_person_id


@pytest.mark.parametrize("raw,expected", [
    ("BMXM-2HZ", "BMXM-2HZ"),
    ("GKLV-DY8", "GKLV-DY8"),
    ("KHZV-H69", "KHZV-H69"),
    ("gklv-dy8", "GKLV-DY8"),
    ("  gklv-dy8\n", "GKLV-DY8"),
    ("ABCD-12345", "ABCD-12345"),
])
def test_valid_ids(raw, expected):
    assert validate_person_id(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "nope", "../users/current", "GKLV-DY8/../x", "GKLV-DY", "GKLV-DY8888",
    "GKL-DY8", "GKLV_DY8", "GKLV-DY8\nX", None, 123, ["GKLV-DY8"], b"GKLV-DY8",
])
def test_invalid_ids(raw):
    with pytest.raises(ValueError, match="GKLV-DY8"):
        validate_person_id(raw)


def test_clamp():
    assert clamp(9999, 1, 50) == 50
    assert clamp(0, 1, 50) == 1
    assert clamp(-5, 0, 10**9) == 0
    assert clamp(7, 1, 50) == 7
    assert clamp("12", 1, 50) == 12
    assert clamp(None, 1, 50) == 1
    assert clamp("abc", 1, 50) == 1
    assert clamp([], 1, 50) == 1
