from __future__ import annotations

from pathlib import Path

WORKFLOW_MD = Path(__file__).parent.parent / "WORKFLOW.md"


def _template() -> str:
    text = WORKFLOW_MD.read_text()
    # Strip YAML frontmatter
    parts = text.split("---", 2)
    return parts[2] if len(parts) == 3 else text


def test_efficiency_rules_section_present():
    assert "## Efficiency rules" in _template()


def test_efficiency_rules_read_once():
    assert "Read each file **once**" in _template()


def test_efficiency_rules_turn_budget():
    assert "20-turn budget" in _template()


def test_efficiency_rules_no_skills():
    assert "Do not invoke brainstorming or planning skills" in _template()


def test_efficiency_rules_file_count_gate():
    assert "read more than 5 files" in _template()
