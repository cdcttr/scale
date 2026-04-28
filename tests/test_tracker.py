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


import respx
import httpx
from symphony.tracker.github import GitHubClient, _slugify, _parse_priority
from symphony.config.schema import TrackerConfig

def _config(**kwargs) -> TrackerConfig:
    defaults = dict(kind="github", repo="owner/repo", api_token="tok")
    defaults.update(kwargs)
    return TrackerConfig(**defaults)

def test_slugify_basic():
    assert _slugify("Hello World!") == "hello-world"

def test_slugify_long_title():
    title = "a" * 100
    assert len(_slugify(title)) <= 50

def test_parse_priority_found():
    assert _parse_priority(["bug", "priority:2"]) == 2

def test_parse_priority_not_found():
    assert _parse_priority(["bug", "enhancement"]) is None

def _gh_issue(number=42, title="Fix bug", state="open", labels=None, node_id="node42"):
    return {
        "node_id": node_id,
        "number": number,
        "title": title,
        "body": "Description here",
        "state": state,
        "labels": [{"name": l} for l in (labels or [])],
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }

@pytest.mark.asyncio
@respx.mock
async def test_fetch_candidate_issues_returns_active():
    route = respx.get("https://api.github.com/repos/owner/repo/issues")
    route.side_effect = [
        httpx.Response(200, json=[_gh_issue()]),
        httpx.Response(200, json=[]),
    ]
    client = GitHubClient(_config())
    issues = await client.fetch_candidate_issues()
    assert len(issues) == 1
    assert issues[0].number == 42
    assert issues[0].state == "active"

@pytest.mark.asyncio
@respx.mock
async def test_fetch_candidate_issues_skips_prs():
    pr = _gh_issue()
    pr["pull_request"] = {"url": "..."}
    route = respx.get("https://api.github.com/repos/owner/repo/issues")
    route.side_effect = [
        httpx.Response(200, json=[pr]),
        httpx.Response(200, json=[]),
    ]
    client = GitHubClient(_config())
    issues = await client.fetch_candidate_issues()
    assert issues == []

@pytest.mark.asyncio
@respx.mock
async def test_state_resolution_closed_is_terminal():
    route = respx.get("https://api.github.com/repos/owner/repo/issues")
    route.side_effect = [
        httpx.Response(200, json=[_gh_issue(state="closed")]),
        httpx.Response(200, json=[]),
    ]
    client = GitHubClient(_config())
    issues = await client.fetch_terminal_issues()
    assert issues[0].state == "terminal"

def test_state_resolution_terminal_label():
    client = GitHubClient(_config(terminal_labels=["symphony:done"]))
    state = client._resolve_state(["symphony:done"], "open")
    assert state == "terminal"

def test_state_resolution_skip_label():
    client = GitHubClient(_config(skip_labels=["wontfix"]))
    state = client._resolve_state(["wontfix"], "open")
    assert state == "ignored"

def test_state_resolution_active_labels_required():
    client = GitHubClient(_config(active_labels=["symphony:active"]))
    assert client._resolve_state([], "open") == "ignored"
    assert client._resolve_state(["symphony:active"], "open") == "active"

def test_identifier_format():
    client = GitHubClient(_config())
    issue = client._normalize(_gh_issue(number=42, title="Fix bug"))
    assert issue.identifier == "owner/repo#42"
    assert issue.branch_name == "symphony/42-fix-bug"
