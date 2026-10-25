"""The README documents the generated API and the commands/flags that really exist."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()
DISCOVERY = (ROOT / "docs" / "api-discovery.md").read_text()


def _section() -> str:
    start = README.index("## Generated API")
    match = re.search(r"^## ", README[start + 1:], re.MULTILINE)
    return README[start:start + 1 + match.start()] if match else README[start:]


def test_section_names_the_api_commands_and_flags():
    section = _section()
    for name in (
        "FamilySearchApi",
        "python -m tools.spec.generate",
        "python -m tools.spec.coverage",
        "python -m tools.verify",
        "allow_unverified",
        "allow_production",
        "confirm_destructive",
        "FSClient",
    ):
        assert name in section, name


def test_section_states_the_status_vocabulary_and_beta_only_meaning():
    section = _section()
    for status in ("verified", "stale", "unverified", "unverifiable", "excluded"):
        assert status in section, status
    assert "not production" in section


def test_section_names_the_verified_operations_and_production_flag():
    section = _section()
    for op in ("createPerson", "readPerson", "deletePerson", "restorePerson"):
        assert op in section, op
    assert "allow_production=True" in section
    assert "still pending" not in section


def test_every_named_module_command_is_importable():
    commands = set(re.findall(r"python -m (tools(?:\.\w+)+)", README + DISCOVERY))
    assert {"tools.spec.generate", "tools.spec.coverage", "tools.verify"} <= commands
    for module in commands:
        assert importlib.util.find_spec(module) is not None, module
    assert importlib.util.find_spec("tools.verify.__main__") is not None


def test_discovery_doc_points_at_the_readme_and_the_regenerate_rule():
    assert "Generated API" in DISCOVERY
    assert "python -m tools.spec.generate" in DISCOVERY
    assert "not by hand" in DISCOVERY
