from __future__ import annotations
from typing import Optional

from symphony.config.schema import WorkflowConfig
from symphony.orchestrator.state import OrchestratorState
from symphony.tracker.models import Issue

_NO_PRIORITY = 999


def is_eligible(
    issue: Issue, state: OrchestratorState, config: WorkflowConfig
) -> bool:
    if issue.state != "active":
        return False
    if issue.id in state.claimed or issue.id in state.running:
        return False
    if len(state.running) >= config.agent.max_concurrent_agents:
        return False
    per_state = config.agent.max_concurrent_agents_by_state
    if per_state:
        limit = per_state.get(issue.state, config.agent.max_concurrent_agents)
        count = sum(
            1 for s in state.running.values() if s.issue.state == issue.state
        )
        if count >= limit:
            return False
    return True


def sort_issues(issues: list[Issue]) -> list[Issue]:
    return sorted(
        issues,
        key=lambda i: (
            i.priority if i.priority is not None else _NO_PRIORITY,
            i.created_at,
            i.number,
        ),
    )


def retry_delay_ms(
    attempt: Optional[int],
    max_ms: int = 300_000,
) -> int:
    if attempt is None:
        return 1_000
    delay = 10_000 * (2 ** (attempt - 1))
    return min(delay, max_ms)
