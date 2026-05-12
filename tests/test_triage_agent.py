import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scale.triage.agent import TriageAgent, TriageAssessment
from scale.tracker.models import Issue
from scale.config.schema import CodexConfig, TriageConfig
from scale.agent.claude import TurnResult


def _config(**kwargs) -> TriageConfig:
    return TriageConfig(**kwargs)


def _codex() -> CodexConfig:
    return CodexConfig()


def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="o/r#1", number=1, title="Add dark mode",
        description="Add a dark mode toggle to the settings page.",
        state="active", labels=[], branch_name="symphony/1-add-dark-mode",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 2),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def _turn_result(text: str, success: bool = True) -> TurnResult:
    return TurnResult(success=success, usage=None, message=text)


@pytest.mark.asyncio
async def test_assess_returns_ready_assessment(tmp_path: Path):
    payload = json.dumps({
        "ready": True,
        "summary": "Clear and actionable.",
        "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**\n\nClear.",
    })
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is not None
    assert result.ready is True
    assert result.summary == "Clear and actionable."
    assert result.reasons == []


@pytest.mark.asyncio
async def test_assess_returns_not_ready_assessment(tmp_path: Path):
    payload = json.dumps({
        "ready": False,
        "summary": "Missing acceptance criteria.",
        "reasons": ["No acceptance criteria", "Vague scope"],
        "comment": "## Symphony Triage\n\n**Status: Needs more detail ❌**",
    })
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is not None
    assert result.ready is False
    assert "No acceptance criteria" in result.reasons


@pytest.mark.asyncio
async def test_assess_handles_runner_failure(tmp_path: Path):
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(side_effect=Exception("network error"))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_handles_turn_not_success(tmp_path: Path):
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result("crashed", success=False))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_handles_bad_json(tmp_path: Path):
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result("not json"))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is None


def test_build_prompt_includes_title_and_body():
    agent = TriageAgent(_config(), _codex())
    issue = _issue(title="Fix login bug", description="Login fails on Safari.")
    prompt = agent._build_prompt(issue, [])
    assert "Fix login bug" in prompt
    assert "Login fails on Safari." in prompt


def test_build_prompt_truncates_comments_to_20():
    agent = TriageAgent(_config(), _codex())
    comments = [
        {"user": {"login": f"user{i}"}, "body": f"comment {i}"}
        for i in range(25)
    ]
    prompt = agent._build_prompt(_issue(), comments)
    assert "comment 24" in prompt
    assert "comment 4" not in prompt


@pytest.mark.asyncio
async def test_assess_passes_model_to_runner(tmp_path: Path):
    payload = json.dumps({
        "ready": True, "summary": "OK.", "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**",
    })
    agent = TriageAgent(_config(model="claude-sonnet-4-6"), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))) as mock_run:
        await agent.assess(_issue(), [], tmp_path)
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs.get("model") == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_assess_returns_needs_approval_assessment(tmp_path: Path):
    payload = json.dumps({
        "ready": False,
        "needs_approval": True,
        "summary": "Well-specified but touches core orchestration.",
        "reasons": ["Touches core dispatch loop"],
        "comment": "## Symphony Triage\n\n**Status: Needs Approval ⚠️**",
    })
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is not None
    assert result.ready is False
    assert result.needs_approval is True
    assert result.summary == "Well-specified but touches core orchestration."


@pytest.mark.asyncio
async def test_assess_needs_approval_false_by_default(tmp_path: Path):
    payload = json.dumps({
        "ready": True,
        "summary": "Clear and actionable.",
        "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**",
    })
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is not None
    assert result.needs_approval is False


def test_system_prompt_mentions_needs_approval():
    from scale.triage.agent import _SYSTEM_PROMPT
    assert "needs_approval" in _SYSTEM_PROMPT or "needs-approval" in _SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_assess_needs_approval_parses_solutions(tmp_path: Path):
    payload = json.dumps({
        "ready": False,
        "needs_approval": True,
        "summary": "Multiple valid approaches exist.",
        "reasons": ["Multiple valid implementation approaches"],
        "solutions": [
            {"name": "Option A — mock the writer", "trade_offs": "low risk, isolated", "recommended": True},
            {"name": "Option B — configurable path", "trade_offs": "more flexible, touches more code", "recommended": False},
        ],
        "comment": "## Symphony Triage\n\n**Status: Needs Approval ⚠️**\n\n## Solutions\n\n**Option A**",
    })
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is not None
    assert result.needs_approval is True
    assert len(result.solutions) == 2
    assert result.solutions[0]["name"] == "Option A — mock the writer"
    assert result.solutions[0]["recommended"] is True
    assert result.solutions[1]["recommended"] is False


@pytest.mark.asyncio
async def test_assess_solutions_defaults_to_empty(tmp_path: Path):
    payload = json.dumps({
        "ready": True,
        "summary": "Clear and actionable.",
        "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**\n\nClear.",
    })
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is not None
    assert result.solutions == []


def test_system_prompt_includes_solutions_for_needs_approval():
    from scale.triage.agent import _SYSTEM_PROMPT
    assert "solutions" in _SYSTEM_PROMPT.lower()


def test_system_prompt_needs_approval_comment_has_solutions_section():
    from scale.triage.agent import _SYSTEM_PROMPT
    assert "## Solutions" in _SYSTEM_PROMPT
