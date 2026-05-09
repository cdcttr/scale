from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from scale.dashboard.ui import _elapsed, _fmt_tokens, _build_table
from scale.orchestrator.state import (
    LiveSession, OrchestratorState, RetryEntry, TokenTotals,
)
from scale.tracker.models import Issue


def _issue(number: int = 1) -> Issue:
    return Issue(
        id=f"i{number}", identifier=f"o/r#{number}", number=number,
        title="Fix the thing", description="", state="active",
        labels=[], branch_name=f"symphony/{number}-fix",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )


def _orch(state: OrchestratorState) -> MagicMock:
    orch = MagicMock()
    orch.get_state.return_value = state
    return orch


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def test_fmt_tokens_small():
    assert _fmt_tokens(0) == "0"
    assert _fmt_tokens(999) == "999"


def test_fmt_tokens_large():
    assert _fmt_tokens(1000) == "1.0k"
    assert _fmt_tokens(12400) == "12.4k"


def test_elapsed_minutes():
    dt = datetime.now(tz=timezone.utc) - timedelta(seconds=125)
    result = _elapsed(dt)
    assert "m" in result
    assert "s" in result
    assert "h" not in result


def test_elapsed_hours():
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=2, minutes=15, seconds=30)
    result = _elapsed(dt)
    assert "h" in result


# ---------------------------------------------------------------------------
# Table building
# ---------------------------------------------------------------------------

def test_build_table_empty_state():
    table = _build_table(_orch(OrchestratorState()))
    assert table is not None


def test_build_table_with_running_session():
    state = OrchestratorState()
    task = MagicMock()
    session = LiveSession(issue=_issue(), task=task)
    session.tokens = TokenTotals(input_tokens=500, output_tokens=200)
    state.running["i1"] = session

    table = _build_table(_orch(state))
    assert table is not None


def test_build_table_with_large_token_counts():
    state = OrchestratorState()
    task = MagicMock()
    session = LiveSession(issue=_issue(), task=task)
    session.tokens = TokenTotals(input_tokens=15000, output_tokens=3000)
    state.running["i1"] = session

    table = _build_table(_orch(state))
    assert table is not None


def test_build_table_with_retry_entry():
    state = OrchestratorState()
    entry = RetryEntry(
        issue=_issue(),
        attempt=2,
        due_at=datetime.now(tz=timezone.utc) + timedelta(seconds=60),
        error="stall timeout",
    )
    state.retry_queue.append(entry)

    table = _build_table(_orch(state))
    assert table is not None


def test_build_table_with_overdue_retry():
    state = OrchestratorState()
    entry = RetryEntry(
        issue=_issue(),
        attempt=3,
        due_at=datetime.now(tz=timezone.utc) - timedelta(seconds=10),
        error="network error",
    )
    state.retry_queue.append(entry)

    table = _build_table(_orch(state))
    assert table is not None
