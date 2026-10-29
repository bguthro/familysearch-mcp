import json
import plistlib
from pathlib import Path

import pytest

from familysearch_import.__main__ import main
from familysearch_import.record import repo_root
from familysearch_import.report import build_report


def _pdf(path):
    pytest.importorskip("pypdf")
    from tests.unit.test_import_embedded import make_pdf

    make_pdf(path)


def make(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    line = root / "Line A"
    dead = line / "1844-1915 Dead Example"
    dead.mkdir(parents=True)
    _pdf(dead / "census.pdf")
    (dead / "ancestry thing.pdf").write_bytes(b"x")
    (dead / "Citation: foo.pdf").write_bytes(b"x")
    (dead / "ResearchSummary.md").write_text("x")
    (dead / "a.webloc").write_bytes(
        plistlib.dumps({"URL": "https://www.familysearch.org/tree/person/details/ABCD-123"})
    )
    live = line / "1960 Livingston Zzliving"
    live.mkdir()
    (live / "birth certificate.pdf").write_bytes(b"x")
    (live / "obituary.pdf").write_bytes(b"x")
    unk = line / "?-? Unknown Example"
    unk.mkdir()
    (unk / "x.pdf").write_bytes(b"x")
    # birth 1850 without death year -> unknown, (born <1900)
    reviewed = line / "1950 Marked Deceased"
    reviewed.mkdir()
    (reviewed / "ImportRecord.json").write_text(
        json.dumps({
            "version": 1, "status": "deceased", "familysearch_id": None, "names": [],
            "gender": None, "facts": [], "relationships": [], "sources": [], "reviewed": True,
        })
    )
    _pdf(reviewed / "will.pdf")
    (line / "No Dates Folder").mkdir()
    return root


def test_report_numbers(tmp_path):
    root = make(tmp_path)
    text = build_report(root, repo_root())
    assert "People scanned: 4" in text
    assert "| Line A | 2 | 1 | 1 | 4 |" in text
    assert "With exactly one id: 1" in text
    assert "Without an id: 3" in text
    assert "Line A / No Dates Folder" in text
    assert "Line A / ?-? Unknown Example" in text
    assert "Dead Example" in text
    # totals: include census+x.pdf(unknown skipped)+will ; living 2 skipped, unknown 1 skipped
    assert "Totals: include 2, cite-only 0, skip 5" in text
    assert "banned provider, no original source: 1" in text
    assert "living: 2" in text
    assert "unknown status: 1" in text
    assert "citation sheet: 1" in text
    assert "Line A / 1844-1915 Dead Example (missing)" in text
    assert "Marked Deceased (reviewed)" not in text


def test_living_only_counts(tmp_path):
    text = build_report(make(tmp_path), repo_root())
    assert "Livingston" not in text and "Zzliving" not in text
    assert "birth certificate" not in text and "obituary" not in text
    assert "Living people: 1" in text


def test_cli_report_default_out(tmp_path, capsys):
    root = make(tmp_path)
    assert main(["report", "--archive", str(root)]) == 0
    out = root / "_import" / "InventoryReport.md"
    assert out.is_file() and "Import Inventory Report" in out.read_text()
    # report file does not change a re-scan
    assert main(["report", "--archive", str(root)]) == 0


def test_cli_out_inside_repo(tmp_path, capsys):
    root = make(tmp_path)
    target = repo_root() / "tests" / "zz_out_should_not_exist" / "R.md"
    assert main(["report", "--archive", str(root), "--out", str(target)]) == 2
    assert not target.parent.exists()
    assert "inside the repo" in capsys.readouterr().err


def test_cli_missing_archive(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert main(["report", "--archive", str(missing)]) == 2
    assert not missing.exists()
    assert main(["drafts", "--archive", str(missing), "--line", "x"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_cli_archive_inside_repo(capsys):
    assert main(["report", "--archive", str(repo_root() / "tests")]) == 2


def test_cli_drafts(tmp_path, capsys):
    root = make(tmp_path)
    assert main(["drafts", "--archive", str(root), "--line", "Line A"]) == 0
    line = root / "Line A"
    assert (line / "1844-1915 Dead Example" / "ImportRecord.draft.json").is_file()
    assert not (line / "?-? Unknown Example" / "ImportRecord.draft.json").exists()
    assert not (line / "1960 Livingston Zzliving" / "ImportRecord.draft.json").exists()
    assert not (line / "1950 Marked Deceased" / "ImportRecord.draft.json").exists()
    # second run writes nothing new and does not fail
    assert main(["drafts", "--archive", str(root), "--line", "Line A"]) == 0
    assert "drafts written: 0" in capsys.readouterr().out


def test_cli_out_refuses_existing_and_archive_paths(tmp_path, capsys):
    root = make(tmp_path)
    existing = tmp_path / "exists.md"
    existing.write_text("keep")
    assert main(["report", "--archive", str(root), "--out", str(existing)]) == 2
    assert existing.read_text() == "keep"
    inside = root / "Line A" / "R.md"
    assert main(["report", "--archive", str(root), "--out", str(inside)]) == 2
    assert not inside.exists()
    ok = root / "_import" / "Other.md"
    assert main(["report", "--archive", str(root), "--out", str(ok)]) == 0
    assert ok.is_file()
    outside = tmp_path / "elsewhere" / "R.md"
    outside.parent.mkdir()
    assert main(["report", "--archive", str(root), "--out", str(outside)]) == 0


def test_default_out_overwrite_allowed(tmp_path):
    root = make(tmp_path)
    assert main(["report", "--archive", str(root)]) == 0
    default = root / "_import" / "InventoryReport.md"
    default.write_text("old")
    assert main(["report", "--archive", str(root), ]) == 0
    assert "Import Inventory Report" in default.read_text()
    assert main(["report", "--archive", str(root), "--out", str(default)]) == 0


def test_unreviewed_deceased_record_stays_living(tmp_path):
    root = tmp_path / "a"
    f = root / "L" / "1950 Pending Person"
    f.mkdir(parents=True)
    (f / "ImportRecord.json").write_text(
        json.dumps({
            "version": 1, "status": "deceased", "familysearch_id": None, "names": [],
            "gender": None, "facts": [], "relationships": [], "sources": [], "reviewed": False,
        })
    )
    text = build_report(root, repo_root())
    assert "| L | 0 | 1 | 0 | 1 |" in text
    assert "Pending Person" not in text


def test_unreadable_folders_listed(tmp_path):
    import os

    root = tmp_path / "a"
    ok = root / "L" / "1800-1870 Fine Person"
    ok.mkdir(parents=True)
    bad = root / "L" / "1800-1870 Locked Person"
    bad.mkdir()
    bad.chmod(0)
    try:
        if os.access(bad, os.R_OK):
            pytest.skip("cannot make folder unreadable")
        text = build_report(root, repo_root())
    finally:
        bad.chmod(0o755)
    assert "## Unreadable folders" in text
    assert "L/1800-1870 Locked Person" in text
    assert "People scanned: 1" in text
