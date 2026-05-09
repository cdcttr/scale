from __future__ import annotations
from datetime import datetime, timezone, timedelta
from io import StringIO
from unittest.mock import MagicMock

from rich.console import Console

from scale.dashboard.ui import _ago, _elapsed, _fmt_tokens, _build_table, Dashboard
from scale.orchestrator.state import (
    CompletedSession, LiveSession, OrchestratorState, RetryEntry, TokenTotals,
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


def test_build_table_header_says_scale_not_symphony():
    table = _build_table(_orch(OrchestratorState()))
    console = Console(file=StringIO(), width=200, highlight=False)
    console.print(table)
    output = console.file.getvalue()
    assert "Scale" in output
    assert "Symphony" not in output


def test_dashboard_accepts_custom_console():
    console = Console()
    orch = MagicMock()
    dashboard = Dashboard(orch, console=console)
    assert dashboard._console is console


def test_dashboard_creates_default_console_when_none_given():
    orch = MagicMock()
    dashboard = Dashboard(orch)
    assert isinstance(dashboard._console, Console)


# ---------------------------------------------------------------------------
# _ago helper
# ---------------------------------------------------------------------------

def test_ago_seconds():
    dt = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
    result = _ago(dt)
    assert result.endswith("s ago")
    assert "m" not in result


def test_ago_minutes():
    dt = datetime.now(tz=timezone.utc) - timedelta(seconds=130)
    result = _ago(dt)
    assert result.endswith("m ago")
    assert "h" not in result


def test_ago_hours():
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=3)
    result = _ago(dt)
    assert result.endswith("h ago")


# ---------------------------------------------------------------------------
# RECENTLY COMPLETED section
# ---------------------------------------------------------------------------

def _completed_session(number: int = 1, seconds_ago: int = 30) -> CompletedSession:
    return CompletedSession(
        issue=_issue(number),
        turn_count=2,
        tokens=TokenTotals(input_tokens=10000, output_tokens=5000),
        completed_at=datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago),
    )


def test_build_table_with_completed_session():
    state = OrchestratorState()
    state.completed.append(_completed_session())
    table = _build_table(_orch(state))
    assert table is not None


def test_build_table_recently_completed_section_appears():
    state = OrchestratorState()
    state.completed.append(_completed_session(number=11, seconds_ago=120))
    state.completed.append(_completed_session(number=8, seconds_ago=240))

    console = Console(file=StringIO(), width=200, highlight=False)
    console.print(_build_table(_orch(state)))
    output = console.file.getvalue()

    assert "RECENTLY COMPLETED" in output
    assert "#11" in output
    assert "#8" in output


def test_build_table_completed_shows_turn_count_and_tokens():
    state = OrchestratorState()
    cs = CompletedSession(
        issue=_issue(5),
        turn_count=3,
        tokens=TokenTotals(input_tokens=18000, output_tokens=400),
        completed_at=datetime.now(tz=timezone.utc) - timedelta(seconds=10),
    )
    state.completed.append(cs)

    console = Console(file=StringIO(), width=200, highlight=False)
    console.print(_build_table(_orch(state)))
    output = console.file.getvalue()

    assert "3 turns" in output
    assert "18.4k" in output


def test_build_table_totals_uses_total_completed_not_visible_count():
    state = OrchestratorState()
    state.total_completed = 10
    state.token_totals = TokenTotals(input_tokens=50000, output_tokens=9600)

    console = Console(file=StringIO(), width=200, highlight=False)
    console.print(_build_table(_orch(state)))
    output = console.file.getvalue()

    assert "10 completed" in output
    assert "59.6k" in output


def test_build_table_no_recently_completed_section_when_empty():
    state = OrchestratorState()

    console = Console(file=StringIO(), width=200, highlight=False)
    console.print(_build_table(_orch(state)))
    output = console.file.getvalue()

    assert "RECENTLY COMPLETED" not in output


def test_build_table_recently_completed_ordered_most_recent_first():
    state = OrchestratorState()
    state.completed.append(_completed_session(number=1, seconds_ago=240))
    state.completed.append(_completed_session(number=2, seconds_ago=60))

    console = Console(file=StringIO(), width=200, highlight=False)
    console.print(_build_table(_orch(state)))
    output = console.file.getvalue()

    pos1 = output.find("#1")
    pos2 = output.find("#2")
    assert pos2 < pos1


# ---------------------------------------------------------------------------
# Finishing session rendering
# ---------------------------------------------------------------------------

def test_build_table_finishing_session_rendered_dim():
    state = OrchestratorState()
    task = MagicMock()
    session = LiveSession(issue=_issue(), task=task)
    session.finishing = True
    session.tokens = TokenTotals(input_tokens=100, output_tokens=40)
    state.running["i1"] = session

    console = Console(file=StringIO(), width=200, highlight=False, markup=True)
    table = _build_table(_orch(state))
    console.print(table)
    output = console.file.getvalue()
    assert table is not None
    assert "Fix the thing" in output


def test_build_table_active_session_not_dim():
    state = OrchestratorState()
    task = MagicMock()
    session = LiveSession(issue=_issue(), task=task)
    session.finishing = False
    state.running["i1"] = session

    table = _build_table(_orch(state))
    assert table is not None


# ---------------------------------------------------------------------------
# Column layout
# ---------------------------------------------------------------------------

def test_issue_number_column_is_narrow():
    table = _build_table(_orch(OrchestratorState()))
    assert table.columns[0].width == 6
    assert table.columns[0].no_wrap is True


def test_title_column_has_ratio():
    table = _build_table(_orch(OrchestratorState()))
    assert table.columns[1].ratio == 1


def test_issue_number_cell_has_no_extra_leading_spaces():
    state = OrchestratorState()
    task = MagicMock()
    session = LiveSession(issue=_issue(17), task=task)
    session.tokens = TokenTotals(input_tokens=0, output_tokens=0)
    state.running["i17"] = session

    table = _build_table(_orch(state))
    col0_cells = [str(c) for c in table.columns[0]._cells]
    assert "#17" in col0_cells
    assert "  #17" not in col0_cells


def test_title_truncated_at_60_chars():
    long_title = "A" * 70
    issue = Issue(
        id="i1", identifier="o/r#1", number=1, title=long_title,
        description="", state="active", labels=[], branch_name="b/1",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )
    state = OrchestratorState()
    task = MagicMock()
    session = LiveSession(issue=issue, task=task)
    session.tokens = TokenTotals(input_tokens=0, output_tokens=0)
    state.running["i1"] = session

    console = Console(file=StringIO(), width=300, highlight=False)
    console.print(_build_table(_orch(state)))
    output = console.file.getvalue()

    assert "A" * 60 in output
    assert "A" * 61 not in output
