import os
import textwrap
import pytest
from pathlib import Path
from pydantic import ValidationError
from symphony.config.schema import (
    WorkflowConfig, TrackerConfig, AgentConfig,
    CodexConfig, WorkerConfig, TriageConfig,
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


from symphony.config.loader import load_workflow, resolve_vars

def test_resolve_vars_substitutes_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret123")
    assert resolve_vars("$MY_TOKEN") == "secret123"

def test_resolve_vars_non_var_passthrough():
    assert resolve_vars("plain-string") == "plain-string"

def test_resolve_vars_missing_env_raises(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(ValueError, match="MISSING_VAR"):
        resolve_vars("$MISSING_VAR")

def test_resolve_vars_nested(monkeypatch):
    monkeypatch.setenv("TOK", "abc")
    data = {"tracker": {"api_token": "$TOK", "repo": "o/r"}}
    result = resolve_vars(data)
    assert result["tracker"]["api_token"] == "abc"

def test_load_workflow_parses_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok123")
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(textwrap.dedent("""\
        ---
        tracker:
          kind: github
          repo: owner/repo
          api_token: $GH_TOKEN
        ---
        You are working on {{ issue.title }}.
    """))
    cfg = load_workflow(wf)
    assert cfg.tracker.api_token == "tok123"
    assert cfg.tracker.repo == "owner/repo"
    assert "{{ issue.title }}" in cfg.prompt_template

def test_load_workflow_resolves_relative_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(textwrap.dedent("""\
        ---
        tracker:
          kind: github
          repo: o/r
          api_token: $GH_TOKEN
        workspace:
          root: ./workspaces
        ---
        prompt
    """))
    cfg = load_workflow(wf)
    assert cfg.workspace.root == str(tmp_path / "workspaces")
    assert os.path.isabs(cfg.workspace.root)


def test_triage_config_defaults():
    cfg = TriageConfig()
    assert cfg.model == "claude-haiku-4-5-20251001"
    assert cfg.ready_label == "symphony:ready"
    assert cfg.needs_detail_label == "symphony:needs-detail"
    assert cfg.triaged_label == "symphony:triaged"


def test_workflow_config_triage_optional():
    wf = WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
    )
    assert wf.triage is None


def test_workflow_config_triage_set():
    wf = WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
        triage=TriageConfig(model="claude-sonnet-4-6"),
    )
    assert wf.triage is not None
    assert wf.triage.model == "claude-sonnet-4-6"
    assert wf.triage.ready_label == "symphony:ready"
    assert wf.triage.triaged_label == "symphony:triaged"


from symphony.config.schema import PlannerConfig

def test_planner_config_defaults():
    cfg = PlannerConfig()
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.max_depth == 3
    assert cfg.plan_label == "symphony:plan"
    assert cfg.leaf_label == "symphony:leaf"
    assert cfg.concept_label == "symphony:concept"
    assert cfg.planned_label == "symphony:planned"
    assert cfg.planner_workspace == "./workspaces/_planner"


def test_workflow_config_planner_defaults_none():
    from symphony.config.schema import WorkflowConfig, TrackerConfig
    cfg = WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        prompt_template="t",
    )
    assert cfg.planner is None


def test_workflow_config_with_planner():
    from symphony.config.schema import WorkflowConfig, TrackerConfig
    cfg = WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        prompt_template="t",
        planner=PlannerConfig(model="claude-opus-4-7", max_depth=2),
    )
    assert cfg.planner is not None
    assert cfg.planner.model == "claude-opus-4-7"
    assert cfg.planner.max_depth == 2
