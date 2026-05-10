import pytest
from datetime import datetime
from scale.tracker.models import Issue

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
from scale.tracker.github import GitHubClient, _slugify, _parse_priority
from scale.config.schema import TrackerConfig

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


from urllib.parse import quote as _url_quote


@pytest.mark.asyncio
@respx.mock
async def test_fetch_issue_comments_returns_list():
    route = respx.get("https://api.github.com/repos/owner/repo/issues/42/comments")
    route.side_effect = [
        httpx.Response(200, json=[
            {"id": 1, "body": "comment text", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z"},
        ]),
        httpx.Response(200, json=[]),
    ]
    client = GitHubClient(_config())
    comments = await client.fetch_issue_comments(42)
    assert len(comments) == 1
    assert comments[0]["body"] == "comment text"


@pytest.mark.asyncio
@respx.mock
async def test_post_comment_sends_body():
    import json as _json
    route = respx.post("https://api.github.com/repos/owner/repo/issues/42/comments").mock(
        return_value=httpx.Response(201, json={"id": 99, "body": "hello"})
    )
    client = GitHubClient(_config())
    await client.post_comment(42, "hello")
    assert route.called
    sent = _json.loads(route.calls[0].request.content)
    assert sent["body"] == "hello"


@pytest.mark.asyncio
@respx.mock
async def test_add_labels_sends_labels():
    import json as _json
    route = respx.post("https://api.github.com/repos/owner/repo/issues/42/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = GitHubClient(_config())
    await client.add_labels(42, ["symphony:ready", "symphony:triaged"])
    assert route.called
    sent = _json.loads(route.calls[0].request.content)
    assert "symphony:ready" in sent["labels"]


@pytest.mark.asyncio
@respx.mock
async def test_remove_label_success():
    encoded = _url_quote("symphony:ready", safe="")
    route = respx.delete(
        f"https://api.github.com/repos/owner/repo/issues/42/labels/{encoded}"
    ).mock(return_value=httpx.Response(200, json=[]))
    client = GitHubClient(_config())
    await client.remove_label(42, "symphony:ready")
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_remove_label_404_is_silent():
    encoded = _url_quote("symphony:ready", safe="")
    route = respx.delete(
        f"https://api.github.com/repos/owner/repo/issues/42/labels/{encoded}"
    ).mock(return_value=httpx.Response(404))
    client = GitHubClient(_config())
    result = await client.remove_label(42, "symphony:ready")
    assert route.called
    assert result is None


@pytest.mark.asyncio
async def test_create_issue():
    import json
    with respx.mock:
        route = respx.post("https://api.github.com/repos/owner/repo/issues").mock(
            return_value=httpx.Response(201, json={
                "number": 99, "node_id": "node99", "title": "New child",
                "html_url": "https://github.com/owner/repo/issues/99",
                "state": "open", "labels": [], "body": "desc",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            })
        )
        gh = GitHubClient(_config())
        result = await gh.create_issue("New child", "desc", ["symphony:ready"])
        # Verify the request body was correct
        sent = json.loads(route.calls[0].request.content)
        assert sent["title"] == "New child"
        assert sent["body"] == "desc"
        assert "symphony:ready" in sent["labels"]
    assert result["number"] == 99
    assert result["node_id"] == "node99"


@pytest.mark.asyncio
async def test_add_sub_issue_success():
    import json
    with respx.mock:
        route = respx.post(
            "https://api.github.com/repos/owner/repo/issues/42/sub_issues"
        ).mock(return_value=httpx.Response(200, json={}))
        gh = GitHubClient(_config())
        result = await gh.add_sub_issue(42, "node99")
        sent = json.loads(route.calls[0].request.content)
        assert sent["sub_issue_id"] == "node99"
    assert result is True


@pytest.mark.asyncio
async def test_add_sub_issue_unavailable():
    with respx.mock:
        respx.post(
            "https://api.github.com/repos/owner/repo/issues/42/sub_issues"
        ).mock(return_value=httpx.Response(404, json={}))
        gh = GitHubClient(_config())
        result = await gh.add_sub_issue(42, "node99")
    assert result is False


@pytest.mark.asyncio
async def test_fetch_sub_issues_success():
    with respx.mock:
        respx.get(
            "https://api.github.com/repos/owner/repo/issues/42/sub_issues"
        ).mock(return_value=httpx.Response(200, json=[
            {"number": 51, "state": "open"},
            {"number": 52, "state": "closed"},
        ]))
        gh = GitHubClient(_config())
        result = await gh.fetch_sub_issues(42)
    assert len(result) == 2
    assert result[0]["number"] == 51


@pytest.mark.asyncio
async def test_fetch_sub_issues_unavailable():
    with respx.mock:
        respx.get(
            "https://api.github.com/repos/owner/repo/issues/42/sub_issues"
        ).mock(return_value=httpx.Response(404, json={}))
        gh = GitHubClient(_config())
        result = await gh.fetch_sub_issues(42)
    assert result == []


@pytest.mark.asyncio
async def test_fetch_issues_by_label():
    issue_data = _gh_issue(number=10, labels=["symphony:planned"])
    with respx.mock:
        route = respx.get("https://api.github.com/repos/owner/repo/issues")
        route.side_effect = [
            httpx.Response(200, json=[issue_data]),
            httpx.Response(200, json=[]),
        ]
        gh = GitHubClient(_config())
        results = await gh.fetch_issues_by_label("symphony:planned")
        assert route.calls[0].request.url.params["labels"] == "symphony:planned"
    assert len(results) == 1
    assert results[0].number == 10


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pr_checks_returns_check_runs():
    respx.get("https://api.github.com/repos/owner/repo/pulls/7").mock(
        return_value=httpx.Response(200, json={"head": {"sha": "abc123"}})
    )
    respx.get("https://api.github.com/repos/owner/repo/commits/abc123/check-runs").mock(
        return_value=httpx.Response(200, json={
            "check_runs": [
                {"id": 1, "status": "completed", "conclusion": "success", "name": "CI"},
            ]
        })
    )
    gh = GitHubClient(_config())
    checks = await gh.fetch_pr_checks(7)
    assert len(checks) == 1
    assert checks[0]["conclusion"] == "success"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pr_checks_returns_empty_when_no_runs():
    respx.get("https://api.github.com/repos/owner/repo/pulls/7").mock(
        return_value=httpx.Response(200, json={"head": {"sha": "abc123"}})
    )
    respx.get("https://api.github.com/repos/owner/repo/commits/abc123/check-runs").mock(
        return_value=httpx.Response(200, json={"check_runs": []})
    )
    gh = GitHubClient(_config())
    checks = await gh.fetch_pr_checks(7)
    assert checks == []


@pytest.mark.asyncio
@respx.mock
async def test_merge_pr_sends_squash_request():
    import json as _json
    route = respx.put("https://api.github.com/repos/owner/repo/pulls/7/merge").mock(
        return_value=httpx.Response(200, json={"merged": True})
    )
    gh = GitHubClient(_config())
    await gh.merge_pr(7)
    assert route.called
    sent = _json.loads(route.calls[0].request.content)
    assert sent["merge_method"] == "squash"
