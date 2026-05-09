import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from scale.triage.runner import TriageRunner, _parse_triage_timestamp, _needs_triage
from scale.triage.agent import TriageAssessment
from scale.tracker.models import Issue
from scale.config.schema import CodexConfig, TriageConfig


def _config(**kwargs) -> TriageConfig:
    return TriageConfig(**kwargs)


def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="o/r#1", number=1, title="Add dark mode",
        description="Add a dark mode toggle.",
        state="active", labels=[], branch_name="symphony/1-add-dark-mode",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def _triage_comment(ts: datetime) -> dict:
    ts_str = ts.isoformat()
    return {
        "body": f"<!-- symphony-triage {ts_str} -->\n## Symphony Triage\n\n**Status: Ready ✅**",
        "created_at": ts_str,
        "user": {"login": "symphony-bot"},
    }


def test_parse_triage_timestamp_valid():
    body = "<!-- symphony-triage 2026-04-28T14:30:00+00:00 -->\n## Symphony Triage"
    ts = _parse_triage_timestamp(body)
    assert ts is not None
    assert ts.year == 2026
    assert ts.month == 4


def test_parse_triage_timestamp_not_a_triage_comment():
    assert _parse_triage_timestamp("not a triage comment") is None


def test_parse_triage_timestamp_malformed_timestamp():
    assert _parse_triage_timestamp("<!-- symphony-triage BROKEN -->") is None


def test_needs_triage_first_time_no_comments():
    assert _needs_triage(_issue(), [], force=False) is True


def test_needs_triage_stale_issue_updated_after_triage():
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    issue = _issue(updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    comments = [_triage_comment(old_ts)]
    assert _needs_triage(issue, comments, force=False) is True


def test_needs_triage_current_issue_not_updated():
    triage_ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    issue = _issue(updated_at=datetime(2026, 1, 4, tzinfo=timezone.utc))
    comments = [_triage_comment(triage_ts)]
    assert _needs_triage(issue, comments, force=False) is False


def test_needs_triage_force_always_true():
    triage_ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    issue = _issue(updated_at=datetime(2026, 1, 4, tzinfo=timezone.utc))
    comments = [_triage_comment(triage_ts)]
    assert _needs_triage(issue, comments, force=True) is True


@pytest.mark.asyncio
async def test_triage_issue_skips_when_current():
    triage_ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    issue = _issue(updated_at=datetime(2026, 1, 4, tzinfo=timezone.utc))
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = [_triage_comment(triage_ts)]

    runner = TriageRunner(_config(), CodexConfig(), gh)
    await runner.triage_issue(issue, force=False)

    gh.post_comment.assert_not_called()
    gh.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_triage_issue_ready_posts_and_labels():
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), CodexConfig(), gh)
    assessment = TriageAssessment(
        ready=True, summary="Clear.",
        comment="## Symphony Triage\n\n**Status: Ready ✅**",
    )
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.triage_issue(issue)

    gh.post_comment.assert_called_once()
    body_posted = gh.post_comment.call_args[0][1]
    assert "<!-- symphony-triage" in body_posted
    assert "## Symphony Triage" in body_posted

    labels_added = gh.add_labels.call_args[0][1]
    assert "scale:ready" in labels_added
    assert "scale:triaged" in labels_added
    gh.remove_label.assert_called_once_with(issue.number, "scale:needs-detail")


@pytest.mark.asyncio
async def test_triage_issue_not_ready_posts_and_labels():
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), CodexConfig(), gh)
    assessment = TriageAssessment(
        ready=False, summary="Missing criteria.",
        reasons=["No acceptance criteria"],
        comment="## Symphony Triage\n\n**Status: Needs more detail ❌**",
    )
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.triage_issue(issue)

    labels_added = gh.add_labels.call_args[0][1]
    assert "scale:needs-detail" in labels_added
    assert "scale:triaged" in labels_added
    gh.remove_label.assert_called_once_with(issue.number, "scale:ready")


@pytest.mark.asyncio
async def test_triage_issue_dry_run_no_github_calls(capsys):
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), CodexConfig(), gh, dry_run=True)
    assessment = TriageAssessment(
        ready=True, summary="Clear.",
        comment="## Symphony Triage\n\n**Status: Ready ✅**",
    )
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.triage_issue(issue)

    gh.post_comment.assert_not_called()
    gh.add_labels.assert_not_called()
    captured = capsys.readouterr()
    assert "Ready: True" in captured.out


@pytest.mark.asyncio
async def test_triage_issue_skips_on_assessment_failure():
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), CodexConfig(), gh)
    with patch.object(runner._agent, "assess", AsyncMock(return_value=None)):
        await runner.triage_issue(issue)

    gh.post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_run_processes_multiple_issues():
    issues = [_issue(id=f"n{i}", number=i) for i in range(1, 4)]
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), CodexConfig(), gh)
    assessment = TriageAssessment(ready=True, summary="Clear.", comment="## Symphony Triage")
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.run(issues)

    assert gh.post_comment.call_count == 3


@pytest.mark.asyncio
async def test_triage_issue_needs_approval_posts_and_labels():
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), CodexConfig(), gh)
    assessment = TriageAssessment(
        ready=False,
        needs_approval=True,
        summary="Well-specified but risky.",
        reasons=["Touches core orchestration"],
        comment="## Symphony Triage\n\n**Status: Needs Approval ⚠️**",
    )
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.triage_issue(issue)

    gh.post_comment.assert_called_once()
    labels_added = gh.add_labels.call_args[0][1]
    assert "scale:needs-approval" in labels_added
    assert "scale:triaged" in labels_added
    removed_calls = [call[0][1] for call in gh.remove_label.call_args_list]
    assert "scale:ready" in removed_calls
    assert "scale:needs-detail" in removed_calls
