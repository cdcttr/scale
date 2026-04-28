import asyncio
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from symphony.orchestrator.core import Orchestrator
from symphony.config.schema import WorkflowConfig, TrackerConfig, AgentConfig
from symphony.tracker.models import Issue

def _config(**agent_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        agent=AgentConfig(**agent_kwargs),
        prompt_template="Work on {{ issue.title }}.",
    )

def _issue(id_="i1", number=1, state="active") -> Issue:
    return Issue(
        id=id_, identifier=f"o/r#{number}", number=number,
        title="T", description="", state=state,
        labels=[], branch_name="symphony/1-t",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )

@pytest.mark.asyncio
async def test_dispatch_adds_to_running():
    tracker = AsyncMock()
    tracker.fetch_candidate_issues.return_value = [_issue()]
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(max_concurrent_agents=5), tracker)

    async def _noop(issue, attempt):
        await asyncio.sleep(0)

    with patch.object(orch, "_run_worker", side_effect=_noop):
        await orch._tick()

    assert "i1" in orch._state.running or "i1" in orch._state.claimed

@pytest.mark.asyncio
async def test_dispatch_respects_concurrency_limit():
    tracker = AsyncMock()
    issues = [_issue(f"i{i}", i + 1) for i in range(3)]
    tracker.fetch_candidate_issues.return_value = issues
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(max_concurrent_agents=2), tracker)

    dispatched = []

    async def _noop(issue, attempt):
        dispatched.append(issue.id)
        await asyncio.sleep(0)

    with patch.object(orch, "_run_worker", side_effect=_noop):
        await orch._tick()

    total = len(orch._state.running) + len(dispatched)
    assert total <= 2
