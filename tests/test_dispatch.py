import pytest
from datetime import datetime
from symphony.tracker.models import Issue
from symphony.orchestrator.state import OrchestratorState, RetryEntry
from symphony.orchestrator.dispatch import (
    is_eligible, sort_issues, retry_delay_ms,
)
from symphony.config.schema import WorkflowConfig, TrackerConfig, AgentConfig

def _config(**agent_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        agent=AgentConfig(**agent_kwargs),
    )

def _issue(id_="i1", number=1, priority=None, state="active",
           created_at=None) -> Issue:
    return Issue(
        id=id_, identifier=f"o/r#{number}", number=number,
        title="T", description="", state=state, labels=[],
        branch_name="symphony/1-t", url="https://example.com",
        priority=priority,
        created_at=created_at or datetime(2026, 1, number),
        updated_at=datetime(2026, 1, number),
    )

def test_eligible_unclaimed_under_limit():
    state = OrchestratorState()
    cfg = _config(max_concurrent_agents=5)
    assert is_eligible(_issue(), state, cfg) is True

def test_ineligible_when_claimed():
    state = OrchestratorState()
    state.claimed.add("i1")
    cfg = _config(max_concurrent_agents=5)
    assert is_eligible(_issue(id_="i1"), state, cfg) is False

def test_ineligible_when_at_global_limit():
    state = OrchestratorState()
    for i in range(3):
        state.running[f"x{i}"] = object()  # type: ignore
    cfg = _config(max_concurrent_agents=3)
    assert is_eligible(_issue(), state, cfg) is False

def test_ineligible_terminal_state():
    state = OrchestratorState()
    cfg = _config(max_concurrent_agents=10)
    assert is_eligible(_issue(state="terminal"), state, cfg) is False

def test_sort_by_priority():
    issues = [_issue("a", 1, priority=None), _issue("b", 2, priority=1)]
    sorted_ = sort_issues(issues)
    assert sorted_[0].priority == 1

def test_sort_by_created_at_when_same_priority():
    issues = [
        _issue("a", 3, created_at=datetime(2026, 1, 3)),
        _issue("b", 2, created_at=datetime(2026, 1, 2)),
    ]
    sorted_ = sort_issues(issues)
    assert sorted_[0].number == 2  # older first

def test_retry_delay_continuation():
    assert retry_delay_ms(attempt=None) == 1000

def test_retry_delay_first_failure():
    assert retry_delay_ms(attempt=1) == 10_000

def test_retry_delay_second_failure():
    assert retry_delay_ms(attempt=2) == 20_000

def test_retry_delay_capped():
    assert retry_delay_ms(attempt=100, max_ms=300_000) == 300_000
