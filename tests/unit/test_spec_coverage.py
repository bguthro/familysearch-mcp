import json

from tools.spec.coverage import build_report, format_report, load_inputs, main
from tools.spec.resolve import SNAPSHOT_PATH, OVERRIDES_PATH, ResolvedOp, load_overrides, resolve


def op(oid, area="Family Tree Endpoints", kind="read", fp="fp"):
    return ResolvedOp(
        source_operation_id=oid, operation_id=oid, method_name=oid.lower(), area=area,
        module="family_tree", http_method="GET", path="/x", summary="", kind=kind,
        destructive=False, path_params=(), required_query=(), accepts=(), body="none",
        fingerprint=fp,
    )


def registry(**entries):
    return {"schema": 1, "operations": {k: {"generated": v} for k, v in entries.items()}}


OPS = [op("a"), op("b"), op("c", kind="write"), op("d", area="Memories Endpoints"), op("e")]
REG = registry(
    a={"status": "verified", "fingerprint": "fp"},
    b={"status": "verified", "fingerprint": "old"},
    c={"status": "unverifiable", "reason": "never executed"},
    d={"status": "unverified"},
)
EXCLUDED = {"x": "broken upstream"}
AREAS = {"x": "Utilities Endpoints"}


def test_counts_on_synthetic_registry():
    r = build_report(OPS, EXCLUDED, REG, areas=AREAS)
    assert r["total"] == 6
    assert r["by_area"]["Family Tree Endpoints"] == {
        "verified": 1, "stale": 1, "unverifiable": 1, "unverified": 1,
    }
    assert r["by_area"]["Memories Endpoints"] == {"unverified": 1}
    assert r["by_area"]["Utilities Endpoints"] == {"excluded": 1}


def test_stale_counts_as_pending():
    r = build_report(OPS, EXCLUDED, REG, areas=AREAS)
    pending = {p["operation_id"]: p for p in r["pending"]}
    assert pending["b"]["status"] == "stale"
    assert set(pending) == {"b", "d", "e"}


def test_unknown_status_and_reasonless_unverifiable_are_pending():
    reg = registry(a={"status": "bogus"}, b={"status": "unverifiable"})
    r = build_report([op("a"), op("b")], {}, reg)
    assert {p["operation_id"] for p in r["pending"]} == {"a", "b"}


def test_excluded_without_reason_is_pending():
    r = build_report([], {"x": "why", "y": ""}, registry(), areas=AREAS)
    assert [p["operation_id"] for p in r["pending"]] == ["y"]
    assert r["by_area"]["unknown"] == {"excluded": 1}


def test_format_lists_every_pending_operation():
    text = format_report(build_report(OPS, EXCLUDED, REG, areas=AREAS))
    for oid in ("b", "d", "e"):
        assert oid in text
    assert "Family Tree Endpoints" in text


def _registry_args(tmp_path, data):
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(data))
    return ["--registry", str(path)]


def test_require_done_fails_with_pending_and_passes_when_complete(tmp_path):
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    ops, _ = resolve(snapshot, load_overrides(OVERRIDES_PATH))
    entries = {
        o.operation_id: {"generated": {"status": "verified", "fingerprint": o.fingerprint}}
        for o in ops
    }
    args = _registry_args(tmp_path, {"schema": 1, "operations": entries})
    assert main(args + ["--require-done"]) == 0
    entries[ops[0].operation_id]["generated"]["fingerprint"] = "stale"
    args = _registry_args(tmp_path, {"schema": 1, "operations": entries})
    assert main(args + ["--require-done"]) == 1
    assert main(args) == 0


def test_malformed_registry_exits_2(tmp_path, capsys):
    assert main(_registry_args(tmp_path, {"operations": []})) == 2
    assert "malformed" in capsys.readouterr().err


def test_blank_and_non_string_reasons_are_pending():
    reg = registry(a={"status": "unverifiable", "reason": "  "},
                   b={"status": "unverifiable", "reason": 5},
                   c={"status": "unverifiable", "reason": "real"})
    r = build_report([op("a"), op("b"), op("c")], {"x": "  ", "y": 5, "z": "ok"}, reg)
    assert {p["operation_id"] for p in r["pending"]} == {"a", "b", "x", "y"}


def test_missing_or_invalid_registry_exits_2_with_message(tmp_path, capsys):
    assert main(["--registry", str(tmp_path / "nope.json")]) == 2
    assert "error:" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert main(["--registry", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err


def test_real_registry(capsys):
    assert main(["--require-done"]) == 1
    assert "205" in capsys.readouterr().out
    ops, excluded, areas, reg = load_inputs()
    r = build_report(ops, excluded, reg, areas=areas)
    assert r["total"] == 205
    flat = [(s, n) for counts in r["by_area"].values() for s, n in counts.items()]
    assert sum(n for s, n in flat if s == "excluded") == 2
    assert sum(n for s, n in flat if s == "unverifiable") == 4
    assert "unknown" not in r["by_area"]
