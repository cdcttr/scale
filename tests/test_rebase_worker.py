from __future__ import annotations
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from scale.agent.claude import TurnResult, TokenUsage
from scale.config.schema import WorkflowConfig, TrackerConfig, RebaseConfig
from scale.tracker.models import Issue
from scale.worker.rebase import RebaseWorker


def _config() -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
        rebase=RebaseConfig(template="Rebase {{ issue.number }} PR #{{ pr.number }}. Context: {{ conflict_context }}"),
    )


def _issue() -> Issue:
    return Issue(
        id="i1", identifier="o/r#1", number=1,
        title="Add caching", description="desc", state="active",
        labels=["scale:conflict"], branch_name="symphony/1-add-caching",
        url="https://github.com/o/r/issues/1", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


def _mock_workspace(tmp_path: Path) -> MagicMock:
    ws = AsyncMock()
    ws.prepare = AsyncMock(return_value=tmp_path)
    ws.run_before_hook = AsyncMock()
    ws.run_after_hook = AsyncMock()
    return ws


def _mock_github() -> MagicMock:
    gh = AsyncMock()
    gh.fetch_pr_for_branch = AsyncMock(return_value={
        "number": 42, "html_url": "https://github.com/o/r/pull/42"
    })
    gh.fetch_pr_diff = AsyncMock(return_value="--- a/x.py\n+++ b/x.py\n+new line")
    gh.fetch_conflict_context = AsyncMock(return_value="abc1234 Add rate limiting")
    return gh


@pytest.mark.asyncio
async def test_rebase_worker_returns_true_on_success(tmp_path: Path):
    ws = _mock_workspace(tmp_path)
    gh = _mock_github()
    worker = RebaseWorker(ws, gh, _config())
    success_result = TurnResult(success=True, usage=TokenUsage(10, 5), message="Done")
    worker._runner.run_turn = AsyncMock(return_value=success_result)

    result = await worker.run(_issue())

    assert result is True
    ws.run_after_hook.assert_called_once()


@pytest.mark.asyncio
async def test_rebase_worker_returns_false_on_agent_failure(tmp_path: Path):
    ws = _mock_workspace(tmp_path)
    gh = _mock_github()
    worker = RebaseWorker(ws, gh, _config())
    fail_result = TurnResult(success=False, usage=None, message="could not resolve conflicts")
    worker._runner.run_turn = AsyncMock(return_value=fail_result)

    result = await worker.run(_issue())

    assert result is False
    ws.run_after_hook.assert_called_once()


@pytest.mark.asyncio
async def test_rebase_worker_returns_false_when_no_pr(tmp_path: Path):
    ws = _mock_workspace(tmp_path)
    gh = _mock_github()
    gh.fetch_pr_for_branch = AsyncMock(return_value=None)
    worker = RebaseWorker(ws, gh, _config())

    result = await worker.run(_issue())

    assert result is False
    ws.run_after_hook.assert_not_called()


@pytest.mark.asyncio
async def test_rebase_worker_uses_pr_branch_checkout(tmp_path: Path):
    ws = _mock_workspace(tmp_path)
    gh = _mock_github()
    worker = RebaseWorker(ws, gh, _config())
    worker._runner.run_turn = AsyncMock(return_value=TurnResult(success=True, usage=None))

    await worker.run(_issue())

    call_kwargs = ws.run_before_hook.call_args
    script = call_kwargs.kwargs.get("script_override") or call_kwargs.args[1]
    assert "symphony/1-add-caching" in script
    assert "git checkout" in script


@pytest.mark.asyncio
async def test_rebase_worker_passes_conflict_context_to_prompt(tmp_path: Path):
    ws = _mock_workspace(tmp_path)
    gh = _mock_github()
    gh.fetch_conflict_context = AsyncMock(return_value="abc1234 Rename auth module")
    worker = RebaseWorker(ws, gh, _config())

    seen_prompts: list[str] = []

    async def _capture_turn(workspace, prompt, is_continuation, **kwargs):
        seen_prompts.append(prompt)
        return TurnResult(success=True, usage=None)

    worker._runner.run_turn = _capture_turn

    await worker.run(_issue())

    assert len(seen_prompts) == 1
    assert "abc1234 Rename auth module" in seen_prompts[0]
