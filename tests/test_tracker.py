import pytest
from datetime import datetime
from symphony.tracker.models import Issue

def _make_issue(**kwargs) -> Issue:
    defaults = dict(
        id="node1",
        identifier="owner/repo#1",
        number=1,
        title="Fix bug",
        description="A bug",
        state="active",
        labels=[],
        branch_name="symphony/1-fix-bug",
        url="https://github.com/owner/repo/issues/1",
        priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2),
    )
    defaults.update(kwargs)
    return Issue(**defaults)

def test_issue_construction():
    issue = _make_issue()
    assert issue.identifier == "owner/repo#1"
    assert issue.state == "active"

def test_issue_priority_none_by_default():
    issue = _make_issue()
    assert issue.priority is None

def test_issue_with_priority():
    issue = _make_issue(priority=2)
    assert issue.priority == 2
