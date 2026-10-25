import dataclasses
import difflib
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from familysearch.api import _runtime
from familysearch.api import spec as api_spec
from tools.spec import generate
from tools.spec.resolve import load_overrides, resolve

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "spec"
GOLDEN = FIXTURES / "golden"
SNAPSHOT = FIXTURES / "mini_snapshot.json"
OVERRIDES = FIXTURES / "mini_overrides.json"


def mini(overrides=None):
    snapshot = json.loads(SNAPSHOT.read_text())
    ov = load_overrides(OVERRIDES)
    for key, value in (overrides or {}).items():
        ov[key] = {**ov.get(key, {}), **value} if isinstance(value, dict) else value
    return resolve(snapshot, ov)


def render_mini(overrides=None):
    ops, excluded = mini(overrides)
    return generate.render(ops, excluded)


def cli_args(out):
    return ["--snapshot", str(SNAPSHOT), "--overrides", str(OVERRIDES), "--out", str(out)]


def test_golden_mini_snapshot():
    rendered = render_mini()
    assert sorted(rendered) == sorted(p.name for p in GOLDEN.glob("*.py"))
    for name, text in rendered.items():
        assert text == (GOLDEN / name).read_text(encoding="utf-8"), name


def test_render_is_deterministic_and_order_independent():
    ops, excluded = mini()
    first = generate.render(ops, excluded)
    assert generate.render(list(reversed(ops)), excluded) == first
    assert generate.render(ops[1:] + ops[:1], excluded) == first
    assert generate.render(ops, excluded) == first
    assert "authentication.py" not in first
    assert all("\r" not in t and t.endswith("\n") for t in first.values())
    assert all(t.startswith(generate.HEADER + "\n") for t in first.values())


def test_generated_code_compiles_and_imports(tmp_path):
    rendered = render_mini()
    for name, text in rendered.items():
        compile(text, name, "exec")
        (tmp_path / name).write_text(text, encoding="utf-8")
    pkg = types.ModuleType("gen_pkg")
    pkg.__path__ = [str(tmp_path)]
    sys.modules["gen_pkg"] = pkg
    sys.modules["gen_pkg._runtime"] = _runtime
    sys.modules["gen_pkg.spec"] = api_spec
    try:
        mod = importlib.import_module("gen_pkg._api")
    finally:
        for k in [k for k in sys.modules if k == "gen_pkg" or k.startswith("gen_pkg.")]:
            del sys.modules[k]
    api = mod.FamilySearchApi
    assert issubclass(api, _runtime.ApiBase)
    for name in ("read_person", "create_person", "delete_memory"):
        assert callable(getattr(api, name))


def test_check_detects_drift(tmp_path, capsys):
    assert generate.main(cli_args(tmp_path)) == 0
    assert generate.main(["--check", *cli_args(tmp_path)]) == 0
    target = tmp_path / "family_tree.py"
    target.write_text(target.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    capsys.readouterr()
    assert generate.main(["--check", *cli_args(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "family_tree.py" in out
    assert "python -m tools.spec.generate" in out


def test_check_reports_missing_file(tmp_path, capsys):
    assert generate.main(["--check", *cli_args(tmp_path)]) == 1
    assert "_api.py" in capsys.readouterr().out


def _changed_ranges(old, new):
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _block(lines, start_prefix, is_end):
    s = next(i for i, line in enumerate(lines) if line.startswith(start_prefix))
    e = next((i for i in range(s + 1, len(lines)) if is_end(lines[i])), len(lines) - 1)
    return s, e


def test_changing_one_override_changes_only_that_ops_output():
    before = render_mini()
    after = render_mini({"kind": {"readPerson": "write"}})
    assert {n for n in before if before[n] != after[n]} == {"_ops.py", "family_tree.py"}

    old, new = before["family_tree.py"].split("\n"), after["family_tree.py"].split("\n")
    is_def = lambda line: line.startswith("    def ")  # noqa: E731
    so, eo = _block(old, "    def read_person(", is_def)
    sn, en = _block(new, "    def read_person(", is_def)
    changed = _changed_ranges(old, new)
    assert changed
    for i1, i2, j1, j2 in changed:
        assert so <= i1 and i2 <= eo and sn <= j1 and j2 <= en

    old, new = before["_ops.py"].split("\n"), after["_ops.py"].split("\n")
    is_close = lambda line: line == "    ),"  # noqa: E731
    so, eo = _block(old, '    "readPerson": OpSpec(', is_close)
    sn, en = _block(new, '    "readPerson": OpSpec(', is_close)
    changed = _changed_ranges(old, new)
    assert changed
    for i1, i2, j1, j2 in changed:
        assert so <= i1 and i2 <= eo and sn <= j1 and j2 <= en
        assert all(
            "kind=" in line or "fingerprint=" in line for line in old[i1:i2] + new[j1:j2]
        )


def test_write_signature_for_upload():
    text = render_mini({"body_kind": {"createPerson": "upload"}})["family_tree.py"]
    block = text.split("def create_person")[1].split("    def ")[0]
    assert (
        "(self, *, reason: str, files: Mapping[str, Any] | None = None, "
        "data: bytes | None = None, data_content_type: str | None = None, "
        "allow_unverified: bool = False, allow_production: bool = False, "
        "confirm_destructive: bool = False, accept: str | None = None, "
        "query: Mapping[str, Any] | None = None) -> ApiResult:"
    ) in block
    assert "body" not in block.split("return")[0]


def test_rejects_param_names_that_clash():
    ops, excluded = mini()
    for bad_name in ("class", "reason", "query"):
        bad = dataclasses.replace(ops[0], path_params=(bad_name,))
        with pytest.raises(ValueError):
            generate.render([bad], excluded)


def test_reads_have_no_write_params_but_read_post_json_has_body():
    text = render_mini()["family_tree.py"]
    read = text.split("def read_person")[1].split("    def ")[0]
    assert "reason" not in read and "allow_" not in read and "body" not in read
    post = text.split("def match_person_by_example")[1].split("    def ")[0]
    assert (
        "(self, *, body: Any = None, accept: str | None = None, "
        "query: Mapping[str, Any] | None = None) -> ApiResult:"
    ) in post
    assert "reason" not in post and "allow_" not in post
    assert "body=body" in post


def test_docstring_with_control_characters_compiles():
    ops, excluded = mini()
    nasty = dataclasses.replace(ops[0], summary='a\x00b\x1b"c\\ \n\t d"')
    for name, text in generate.render([nasty], excluded).items():
        compile(text, name, "exec")
