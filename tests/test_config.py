import pytest
from pydantic import ValidationError
from symphony.config.schema import (
    WorkflowConfig, TrackerConfig, AgentConfig,
    CodexConfig, WorkerConfig,
)

def test_tracker_config_required_fields():
    with pytest.raises(ValidationError):
        TrackerConfig(kind="github")  # missing repo and api_token

def test_tracker_config_valid():
    t = TrackerConfig(kind="github", repo="owner/repo", api_token="tok")
    assert t.repo == "owner/repo"
    assert t.active_labels == []
    assert t.skip_labels == ["symphony:skip"]
    assert t.terminal_labels == ["symphony:done"]

def test_workflow_config_defaults():
    cfg = WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        prompt_template="hello",
    )
    assert cfg.polling.interval_ms == 30000
    assert cfg.agent.max_concurrent_agents == 10
    assert cfg.agent.max_turns == 20
    assert cfg.codex.command == "claude"
    assert cfg.codex.stall_timeout_ms == 300000
    assert cfg.server is None
    assert cfg.worker.ssh_hosts == []

def test_agent_config_per_state_defaults():
    a = AgentConfig()
    assert a.max_concurrent_agents_by_state == {}

def test_codex_approval_policy_only_auto():
    with pytest.raises(ValidationError):
        CodexConfig(approval_policy="manual")
