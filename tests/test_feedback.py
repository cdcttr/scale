from __future__ import annotations
import asyncio
import pytest
import respx
import httpx
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from scale.config.schema import WorkflowConfig, TrackerConfig, ReviewConfig
from scale.orchestrator.core import Orchestrator
from scale.orchestrator.state import OrchestratorState
from scale.tracker.github import GitHubClient
from scale.tracker.models import Issue
from scale.worker.feedback import FeedbackWorker
from scale.agent.claude import TurnResult, TokenUsage
from scale.prompt.renderer import render_feedback_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(feedback_enabled: bool = True) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="owner/repo", api_token="tok"),
        prompt_template="Work on {{ issue.title }}. {{ pr_feedback }}",
        review=ReviewConfig(feedback_enabled=feedback_enabled),
    )


def _tracker_config() -> TrackerConfig:
    return TrackerConfig(kind="github", repo="owner/repo", api_token="tok")


def _issue(number: int = 1) -> Issue:
    return Issue(
        id=f"i{number}", identifier=f"owner/repo#{number}", number=number,
        title="Fix it", description="desc", state="active",
        labels=["scale:pr-open"], branch_name=f"symphony/{number}-fix-it",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )


def _human_comment(body: str = "Please fix this") -> dict:
    return {
        "id": 1,
        "body": body,
        "user": {"login": "alice"},
        "created_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }


def _stats_comment() -> dict:
    return {
        "id": 2,
        "body": '<!-- scale-stats {"issue": 1} -->\n\n## Scale run complete',
        "user": {"login": "scale-bot"},
        "created_at": "2026-01-01T12:00:00Z",
        "updated_at": "2026-01-01T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# fetch_pr_comments — GitHubClient
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_fetch_pr_comments_returns_list():
    route = respx.get("https://api.github.com/repos/owner/repo/issues/42/comments")
    route.side_effect = [
        httpx.Response(200, json=[_human_comment("feedback here")]),
        httpx.Response(200, json=[]),
    ]
    client = GitHubClient(_tracker_config())
    comments = await client.fetch_pr_comments(42)
    assert len(comments) == 1
    assert comments[0]["body"] == "feedback here"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pr_comments_passes_since_param():
    since = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
    route = respx.get("https://api.github.com/repos/owner/repo/issues/42/comments")
    route.side_effect = [
        httpx.Response(200, json=[]),
    ]
    client = GitHubClient(_tracker_config())
    await client.fetch_pr_comments(42, since=since)
    assert route.calls[0].request.url.params["since"] == "2026-01-02T00:00:00Z"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pr_comments_no_since_omits_param():
    route = respx.get("https://api.github.com/repos/owner/repo/issues/42/comments")
    route.side_effect = [
        httpx.Response(200, json=[]),
    ]
    client = GitHubClient(_tracker_config())
    await client.fetch_pr_comments(42)
    assert "since" not in route.calls[0].request.url.params


# ---------------------------------------------------------------------------
# render_feedback_prompt
# ---------------------------------------------------------------------------

def test_render_feedback_prompt_includes_diff_and_comments():
    issue = _issue()
    result = render_feedback_prompt(
        "{{ issue.title }} {{ pr_feedback }}",
        issue,
        pr_diff="diff --git a/foo.py",
        pr_comments=[_human_comment("Please fix this")],
    )
    assert "Fix it" in result
    assert "diff --git a/foo.py" in result
    assert "Please fix this" in result
    assert "alice" in result


def test_render_feedback_prompt_includes_safety_preamble():
    issue = _issue()
    result = render_feedback_prompt(
        "{{ issue.title }}",
        issue,
        pr_diff="",
        pr_comments=[],
    )
    assert "autonomous coding agent" in result


def test_render_feedback_prompt_multiple_comments():
    issue = _issue()
    comments = [
        {"id": 1, "body": "Change X", "user": {"login": "alice"}, "created_at": "2026-01-02T00:00:00Z"},
        {"id": 2, "body": "Fix Y", "user": {"login": "bob"}, "created_at": "2026-01-02T01:00:00Z"},
    ]
    result = render_feedback_prompt("{{ pr_feedback }}", issue, pr_diff="", pr_comments=comments)
    assert "Change X" in result
    assert "Fix Y" in result
    assert "alice" in result
    assert "bob" in result


# ---------------------------------------------------------------------------
# FeedbackWorker
# ---------------------------------------------------------------------------

def _mock_workspace(tmp_path: Path) -> MagicMock:
    ws = AsyncMock()
    ws.prepare = AsyncMock(return_value=tmp_path)
    ws.run_before_hook = AsyncMock()
    ws.run_after_hook = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_feedback_worker_runs_with_branch_checkout_hook(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = FeedbackWorker(ws, config)
    worker._runner.run_turn = AsyncMock(return_value=TurnResult(success=True, usage=TokenUsage(10, 5)))

    issue = _issue()
    await worker.run(issue, pr_diff="diff here", pr_comments=[_human_comment()])

    ws.run_before_hook.assert_called_once()
    call_kwargs = ws.run_before_hook.call_args
    script = call_kwargs[1].get("script_override") or call_kwargs[0][1]
    assert "git fetch origin" in script
    assert issue.branch_name in script
    assert "git checkout" in script


@pytest.mark.asyncio
async def test_feedback_worker_runs_after_hook_on_failure(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = FeedbackWorker(ws, config)
    worker._runner.run_turn = AsyncMock(return_value=TurnResult(success=False, usage=None, message="crash"))

    with pytest.raises(RuntimeError, match="Feedback turn failed"):
        await worker.run(_issue(), pr_diff="", pr_comments=[])

    ws.run_after_hook.assert_called_once()


@pytest.mark.asyncio
async def test_feedback_worker_passes_log_path_to_runner(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = FeedbackWorker(ws, config)

    captured: list[dict] = []

    async def _capture(workspace, prompt, is_continuation, **kwargs):
        captured.append(kwargs)
        return TurnResult(success=True, usage=TokenUsage(5, 3))

    worker._runner.run_turn = _capture

    await worker.run(_issue(), pr_diff="diff content", pr_comments=[_human_comment("please fix")])

    assert len(captured) == 1
    assert captured[0]["log_path"] == tmp_path / "agent.log"
    assert captured[0]["log_label"] == "Feedback Turn"


# ---------------------------------------------------------------------------
# Orchestrator — _watch_pr_feedback_tick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watch_pr_feedback_initializes_watermark_on_first_encounter():
    tracker = AsyncMock()
    scm = AsyncMock()
    config = _config()
    tracker.fetch_issues_by_label = AsyncMock(return_value=[_issue()])
    orch = Orchestrator(config, tracker, scm=scm)

    issue = _issue()
    await orch._watch_pr_feedback_tick()

    assert issue.number in orch._state.pr_comment_watermarks
    scm.fetch_pr_comments.assert_not_called()


@pytest.mark.asyncio
async def test_watch_pr_feedback_dispatches_on_new_human_comments():
    tracker = AsyncMock()
    scm = AsyncMock()
    config = _config()
    issue = _issue()
    tracker.fetch_issues_by_label = AsyncMock(return_value=[issue])
    scm.fetch_pr_comments = AsyncMock(return_value=[_human_comment()])
    orch = Orchestrator(config, tracker, scm=scm)

    watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
    orch._state.pr_comment_watermarks[issue.number] = watermark

    dispatched = []

    async def _noop(iss, comments):
        dispatched.append(iss.id)

    with patch.object(orch, "_run_feedback_worker", side_effect=_noop):
        await orch._watch_pr_feedback_tick()
        await asyncio.sleep(0)

    assert issue.id in dispatched or issue.id in orch._state.claimed


@pytest.mark.asyncio
async def test_watch_pr_feedback_filters_scale_stats_comments():
    tracker = AsyncMock()
    scm = AsyncMock()
    config = _config()
    issue = _issue()
    tracker.fetch_issues_by_label = AsyncMock(return_value=[issue])
    scm.fetch_pr_comments = AsyncMock(return_value=[_stats_comment()])
    orch = Orchestrator(config, tracker, scm=scm)
    orch._state.pr_comment_watermarks[issue.number] = datetime(2026, 1, 1, tzinfo=timezone.utc)

    dispatched = []

    async def _noop(iss, comments):
        dispatched.append(iss.id)

    with patch.object(orch, "_run_feedback_worker", side_effect=_noop):
        await orch._watch_pr_feedback_tick()
        await asyncio.sleep(0)

    assert issue.id not in dispatched


@pytest.mark.asyncio
async def test_watch_pr_feedback_skips_claimed_issues():
    tracker = AsyncMock()
    scm = AsyncMock()
    config = _config()
    issue = _issue()
    tracker.fetch_issues_by_label = AsyncMock(return_value=[issue])
    scm.fetch_pr_comments = AsyncMock(return_value=[_human_comment()])
    orch = Orchestrator(config, tracker, scm=scm)
    orch._state.claimed.add(issue.id)
    orch._state.pr_comment_watermarks[issue.number] = datetime(2026, 1, 1, tzinfo=timezone.utc)

    dispatched = []

    async def _noop(iss, comments):
        dispatched.append(iss.id)

    with patch.object(orch, "_run_feedback_worker", side_effect=_noop):
        await orch._watch_pr_feedback_tick()
        await asyncio.sleep(0)

    assert issue.id not in dispatched


@pytest.mark.asyncio
async def test_watch_pr_feedback_skips_when_no_new_comments():
    tracker = AsyncMock()
    scm = AsyncMock()
    config = _config()
    issue = _issue()
    tracker.fetch_issues_by_label = AsyncMock(return_value=[issue])
    scm.fetch_pr_comments = AsyncMock(return_value=[])
    orch = Orchestrator(config, tracker, scm=scm)
    orch._state.pr_comment_watermarks[issue.number] = datetime(2026, 1, 1, tzinfo=timezone.utc)

    dispatched = []

    async def _noop(iss, comments):
        dispatched.append(iss.id)

    with patch.object(orch, "_run_feedback_worker", side_effect=_noop):
        await orch._watch_pr_feedback_tick()
        await asyncio.sleep(0)

    assert issue.id not in dispatched


@pytest.mark.asyncio
async def test_watch_pr_feedback_handles_comment_fetch_error():
    tracker = AsyncMock()
    scm = AsyncMock()
    config = _config()
    issue = _issue()
    tracker.fetch_issues_by_label = AsyncMock(return_value=[issue])
    scm.fetch_pr_comments = AsyncMock(side_effect=RuntimeError("network error"))
    orch = Orchestrator(config, tracker, scm=scm)
    orch._state.pr_comment_watermarks[issue.number] = datetime(2026, 1, 1, tzinfo=timezone.utc)

    await orch._watch_pr_feedback_tick()  # must not raise


# ---------------------------------------------------------------------------
# Orchestrator — _run_feedback_worker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_feedback_worker_updates_watermark():
    tracker = AsyncMock()
    scm = AsyncMock()
    scm.fetch_pr_for_branch = AsyncMock(return_value={"number": 10, "html_url": "https://example.com/pr/10"})
    scm.fetch_pr_diff = AsyncMock(return_value="diff content")
    config = _config()
    orch = Orchestrator(config, tracker, scm=scm)
    issue = _issue()
    orch._state.claimed.add(issue.id)

    with patch.object(orch._workspace, "prepare", AsyncMock(return_value=MagicMock())), \
         patch("scale.orchestrator.core.FeedbackWorker") as MockFeedback:
        mock_worker = MagicMock()
        mock_worker.run = AsyncMock()
        MockFeedback.return_value = mock_worker
        await orch._run_feedback_worker(issue, [_human_comment()])

    assert issue.number in orch._state.pr_comment_watermarks
    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_run_feedback_worker_updates_watermark_on_failure():
    tracker = AsyncMock()
    scm = AsyncMock()
    scm.fetch_pr_for_branch = AsyncMock(return_value={"number": 10, "html_url": "..."})
    scm.fetch_pr_diff = AsyncMock(return_value="diff")
    config = _config()
    orch = Orchestrator(config, tracker, scm=scm)
    issue = _issue()
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.FeedbackWorker") as MockFeedback:
        mock_worker = MagicMock()
        mock_worker.run = AsyncMock(side_effect=RuntimeError("agent crashed"))
        MockFeedback.return_value = mock_worker
        await orch._run_feedback_worker(issue, [_human_comment()])  # must not raise

    assert issue.number in orch._state.pr_comment_watermarks
    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_run_feedback_worker_skips_when_no_pr():
    tracker = AsyncMock()
    scm = AsyncMock()
    scm.fetch_pr_for_branch = AsyncMock(return_value=None)
    config = _config()
    orch = Orchestrator(config, tracker, scm=scm)
    issue = _issue()
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.FeedbackWorker") as MockFeedback:
        await orch._run_feedback_worker(issue, [_human_comment()])

    MockFeedback.assert_not_called()
    assert issue.id not in orch._state.claimed


# ---------------------------------------------------------------------------
# Orchestrator.run — feedback loop wired in
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_starts_feedback_loop_when_enabled():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues = AsyncMock(return_value=[])
    scm = AsyncMock()
    config = _config(feedback_enabled=True)
    orch = Orchestrator(config, tracker, scm=scm)

    feedback_started = False

    async def _mock_feedback():
        nonlocal feedback_started
        feedback_started = True
        await asyncio.sleep(0)

    async def _mock_tick():
        await asyncio.sleep(0)

    async def _mock_merge_queue():
        await asyncio.sleep(0)

    with patch.object(orch, "_tick_loop", side_effect=_mock_tick), \
         patch.object(orch, "_watch_merge_queue", side_effect=_mock_merge_queue), \
         patch.object(orch, "_watch_pr_feedback", side_effect=_mock_feedback):
        await orch.run()

    assert feedback_started


@pytest.mark.asyncio
async def test_run_does_not_start_feedback_loop_when_disabled():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues = AsyncMock(return_value=[])
    config = _config(feedback_enabled=False)
    orch = Orchestrator(config, tracker)

    feedback_started = False

    async def _mock_feedback():
        nonlocal feedback_started
        feedback_started = True

    async def _mock_tick():
        await asyncio.sleep(0)

    async def _mock_merge_queue():
        await asyncio.sleep(0)

    with patch.object(orch, "_tick_loop", side_effect=_mock_tick), \
         patch.object(orch, "_watch_merge_queue", side_effect=_mock_merge_queue), \
         patch.object(orch, "_watch_pr_feedback", side_effect=_mock_feedback):
        await orch.run()

    assert not feedback_started


# ---------------------------------------------------------------------------
# ReviewConfig schema
# ---------------------------------------------------------------------------

def test_review_config_feedback_disabled_by_default():
    cfg = ReviewConfig()
    assert cfg.feedback_enabled is False


def test_review_config_feedback_can_be_enabled():
    cfg = ReviewConfig(feedback_enabled=True)
    assert cfg.feedback_enabled is True
