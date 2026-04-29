import json
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from symphony.triage.agent import TriageAgent, TriageAssessment
from symphony.tracker.models import Issue
from symphony.config.schema import TriageConfig


def _config(**kwargs) -> TriageConfig:
    return TriageConfig(**kwargs)


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


def _make_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    return msg


def test_assess_returns_ready_assessment():
    payload = json.dumps({
        "ready": True,
        "summary": "Clear and actionable.",
        "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**\n\nClear.",
    })
    agent = TriageAgent(_config())
    with patch.object(agent._client.messages, "create", return_value=_make_response(payload)):
        result = agent.assess(_issue(), [])
    assert result is not None
    assert result.ready is True
    assert result.summary == "Clear and actionable."
    assert result.reasons == []


def test_assess_returns_not_ready_assessment():
    payload = json.dumps({
        "ready": False,
        "summary": "Missing acceptance criteria.",
        "reasons": ["No acceptance criteria", "Vague scope"],
        "comment": "## Symphony Triage\n\n**Status: Needs more detail ❌**",
    })
    agent = TriageAgent(_config())
    with patch.object(agent._client.messages, "create", return_value=_make_response(payload)):
        result = agent.assess(_issue(), [])
    assert result is not None
    assert result.ready is False
    assert "No acceptance criteria" in result.reasons


def test_assess_handles_api_failure():
    agent = TriageAgent(_config())
    with patch.object(agent._client.messages, "create", side_effect=Exception("network error")):
        result = agent.assess(_issue(), [])
    assert result is None


def test_assess_handles_bad_json():
    agent = TriageAgent(_config())
    with patch.object(agent._client.messages, "create", return_value=_make_response("not json")):
        result = agent.assess(_issue(), [])
    assert result is None


def test_build_prompt_includes_title_and_body():
    agent = TriageAgent(_config())
    issue = _issue(title="Fix login bug", description="Login fails on Safari.")
    prompt = agent._build_prompt(issue, [])
    assert "Fix login bug" in prompt
    assert "Login fails on Safari." in prompt


def test_build_prompt_truncates_comments_to_20():
    agent = TriageAgent(_config())
    comments = [
        {"user": {"login": f"user{i}"}, "body": f"comment {i}"}
        for i in range(25)
    ]
    prompt = agent._build_prompt(_issue(), comments)
    assert "comment 24" in prompt
    assert "comment 4" not in prompt


def test_build_prompt_includes_labels():
    agent = TriageAgent(_config())
    issue = _issue(labels=["bug", "priority:1"])
    prompt = agent._build_prompt(issue, [])
    assert "bug" in prompt
    assert "priority:1" in prompt


def test_assess_uses_configured_model():
    payload = json.dumps({
        "ready": True, "summary": "OK.", "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**",
    })
    agent = TriageAgent(_config(model="claude-sonnet-4-6"))
    with patch.object(agent._client.messages, "create", return_value=_make_response(payload)) as mock_create:
        agent.assess(_issue(), [])
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-4-6"
