import pytest
from datetime import datetime
from scale.tracker.models import Issue
from scale.prompt.renderer import render_prompt, _SAFETY_PREAMBLE

def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="owner/repo#5", number=5,
        title="Add dark mode", description="Make it dark.",
        state="active", labels=["enhancement"],
        branch_name="symphony/5-add-dark-mode",
        url="https://github.com/owner/repo/issues/5",
        priority=1,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2),
    )
    defaults.update(kwargs)
    return Issue(**defaults)

def test_render_always_includes_safety_preamble():
    result = render_prompt("{{ issue.title }}", _issue(), attempt=None)
    assert result.startswith(_SAFETY_PREAMBLE)

def test_render_basic():
    template = "Issue: {{ issue.title }}"
    result = render_prompt(template, _issue(), attempt=None)
    assert "Issue: Add dark mode" in result

def test_render_identifier():
    template = "Ref: {{ issue.identifier }}"
    result = render_prompt(template, _issue(), attempt=None)
    assert "Ref: owner/repo#5" in result

def test_render_labels_join():
    template = "Labels: {{ issue.labels | join: ', ' }}"
    result = render_prompt(template, _issue(), attempt=None)
    assert "Labels: enhancement" in result

def test_render_attempt_none_no_block():
    template = "{% if attempt %}retry {{ attempt }}{% endif %}done"
    result = render_prompt(template, _issue(), attempt=None)
    assert "done" in result
    assert "retry" not in result

def test_render_attempt_integer():
    template = "{% if attempt %}retry {{ attempt }}{% endif %}done"
    result = render_prompt(template, _issue(), attempt=2)
    assert "retry 2" in result

def test_render_unknown_variable_raises():
    template = "{{ unknown_var }}"
    with pytest.raises(Exception):
        render_prompt(template, _issue(), attempt=None)

def test_render_previous_attempt_summary_injected():
    template = "{{ previous_attempt_summary }}"
    result = render_prompt(template, _issue(), attempt=2, previous_attempt_summary="Files changed: foo.py")
    assert "Files changed: foo.py" in result

def test_render_previous_attempt_summary_defaults_to_empty_string():
    template = "Summary: '{{ previous_attempt_summary }}'"
    result = render_prompt(template, _issue(), attempt=None)
    assert "Summary: ''" in result

def test_render_previous_attempt_summary_usable_in_attempt_block():
    template = "{% if attempt %}prev: {{ previous_attempt_summary }}{% endif %}done"
    result = render_prompt(template, _issue(), attempt=1, previous_attempt_summary="modified foo.py")
    assert "prev: modified foo.py" in result
