import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from symphony.planner.agent import PlannerAgent, PlanAssessment, ChildSpec
from symphony.tracker.models import Issue
from symphony.config.schema import CodexConfig, PlannerConfig
from symphony.agent.claude import TurnResult


def _config(**kwargs) -> PlannerConfig:
    return PlannerConfig(**kwargs)


def _codex() -> CodexConfig:
    return CodexConfig()


def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="o/r#1", number=1, title="Build admin dashboard",
        description="We need a full admin dashboard with user management and analytics.",
        state="active", labels=[], branch_name="symphony/1-build-admin-dashboard",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 2),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def _turn(text: str, success: bool = True) -> TurnResult:
    return TurnResult(success=success, usage=None, message=text)


@pytest.mark.asyncio
async def test_assess_leaf_task(tmp_path: Path):
    payload = json.dumps({"type": "leaf", "children": None})
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn(payload))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is not None
    assert result.is_leaf is True
    assert result.children == []


@pytest.mark.asyncio
async def test_assess_concept_with_children(tmp_path: Path):
    payload = json.dumps({
        "type": "concept",
        "children": [
            {"title": "Build user list page", "description": "Create /admin/users", "labels": ["symphony:ready"]},
            {"title": "Build analytics page", "description": "Create /admin/analytics", "labels": ["symphony:ready"]},
        ],
    })
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn(payload))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is not None
    assert result.is_leaf is False
    assert len(result.children) == 2
    assert result.children[0].title == "Build user list page"
    assert "symphony:ready" in result.children[0].labels


@pytest.mark.asyncio
async def test_assess_at_max_depth_returns_leaf(tmp_path: Path):
    agent = PlannerAgent(_config(max_depth=2), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock()) as mock_run:
        result = await agent.assess(_issue(), [], 2, tmp_path)
    assert result is not None
    assert result.is_leaf is True
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_assess_handles_runner_exception(tmp_path: Path):
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(side_effect=Exception("fail"))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_handles_failed_turn(tmp_path: Path):
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn("", success=False))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_handles_bad_json(tmp_path: Path):
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn("not json"))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_passes_model_to_runner(tmp_path: Path):
    payload = json.dumps({"type": "leaf", "children": None})
    agent = PlannerAgent(_config(model="claude-opus-4-7"), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn(payload))) as mock_run:
        await agent.assess(_issue(), [], 0, tmp_path)
    assert mock_run.call_args[1].get("model") == "claude-opus-4-7"


def test_build_prompt_includes_issue_content():
    agent = PlannerAgent(_config(), _codex())
    issue = _issue(title="Add OAuth", description="Support Google OAuth.")
    prompt = agent._build_prompt(issue, [], 0)
    assert "Add OAuth" in prompt
    assert "Support Google OAuth." in prompt
    assert "depth: 0" in prompt
