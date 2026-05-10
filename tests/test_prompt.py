import pytest
from datetime import datetime
from scale.tracker.models import Issue
from scale.prompt.renderer import render_prompt, render_rebase_prompt, _SAFETY_PREAMBLE

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


def _rebase_issue() -> Issue:
    return Issue(
        id="r1", identifier="o/r#5", number=5,
        title="Add caching", description="Cache the responses",
        state="active", labels=[], branch_name="symphony/5-add-caching",
        url="https://github.com/o/r/issues/5", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )

def test_render_rebase_prompt_basic():
    template = "Issue #{{ issue.number }} branch {{ issue.branch_name }} PR #{{ pr.number }}"
    result = render_rebase_prompt(
        template,
        issue=_rebase_issue(),
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
        pr_diff="--- a/x.py\n+++ b/x.py",
        conflict_context="abc123 Add rate limiting",
    )
    assert "Issue #5" in result
    assert "symphony/5-add-caching" in result
    assert "PR #42" in result

def test_render_rebase_prompt_includes_conflict_context():
    template = "Conflict context: {{ conflict_context }}"
    result = render_rebase_prompt(
        template,
        issue=_rebase_issue(),
        pr_number=1,
        pr_url="https://example.com",
        pr_diff="",
        conflict_context="deadbeef Rename auth module",
    )
    assert "deadbeef Rename auth module" in result

def test_render_rebase_prompt_includes_pr_diff():
    template = "Diff: {{ pr.diff }}"
    result = render_rebase_prompt(
        template,
        issue=_rebase_issue(),
        pr_number=1,
        pr_url="https://example.com",
        pr_diff="--- a/x.py\n+++ b/x.py\n+new line",
        conflict_context="",
    )
    assert "+new line" in result

def test_render_rebase_prompt_has_safety_preamble():
    template = "hello"
    result = render_rebase_prompt(
        template,
        issue=_rebase_issue(),
        pr_number=1,
        pr_url="https://example.com",
        pr_diff="",
        conflict_context="",
    )
    assert "autonomous coding agent" in result
