from __future__ import annotations
import asyncio
import json
import logging
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from scale.orchestrator.core import Orchestrator
from scale.orchestrator.state import CompletedSession, LiveSession, RetryEntry, TokenTotals
from scale.config.schema import WorkflowConfig, TrackerConfig, AgentConfig, WorkerConfig, PlannerConfig, PollingConfig, TriageConfig, ReviewConfig
from scale.tracker.models import Issue
from scale.worker.local import LocalWorker
from scale.worker.ssh import SSHWorker

def _config(**agent_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        polling=PollingConfig(interval_ms=0),
        agent=AgentConfig(**agent_kwargs),
        prompt_template="Work on {{ issue.title }}.",
    )

def _config_ssh(*hosts: str) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        polling=PollingConfig(interval_ms=0),
        worker=WorkerConfig(ssh_hosts=list(hosts)),
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


@pytest.mark.asyncio
async def test_token_totals_accumulated_on_success():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        if on_event:
            on_event({"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}})

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", AsyncMock()), \
         patch.object(orch._workspace, "remove", AsyncMock()):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    assert orch._state.running[issue.id].finishing is True
    await orch._flush_finishing()
    assert orch._state.token_totals.input_tokens == 100
    assert orch._state.token_totals.output_tokens == 50


@pytest.mark.asyncio
async def test_token_totals_accumulate_across_sessions():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)

    for i, (inp, out) in enumerate([(100, 50), (200, 80)]):
        issue = _issue(id_=f"i{i}", number=i + 1)
        task = asyncio.create_task(asyncio.sleep(0))
        orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
        orch._state.claimed.add(issue.id)
        inp_cap, out_cap = inp, out

        async def _mock_run(iss, cfg, attempt, on_event=None, _i=inp_cap, _o=out_cap, previous_attempt_summary=None):
            if on_event:
                on_event({"type": "assistant", "message": {"usage": {"input_tokens": _i, "output_tokens": _o}}})

        with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
             patch.object(orch._github, "add_labels", AsyncMock()), \
             patch.object(orch._workspace, "remove", AsyncMock()):
            mock_w = MagicMock()
            mock_w.run = _mock_run
            MockWorker.return_value = mock_w
            await orch._run_worker(issue, attempt=None)

    await orch._flush_finishing()
    assert orch._state.token_totals.input_tokens == 300
    assert orch._state.token_totals.output_tokens == 130


def test_make_worker_returns_local_when_no_ssh():
    orch = Orchestrator(_config(), AsyncMock())
    worker = orch._make_worker()
    assert isinstance(worker, LocalWorker)


def test_make_worker_returns_ssh_when_configured():
    orch = Orchestrator(_config_ssh("user@host1", "user@host2"), AsyncMock())
    w1 = orch._make_worker()
    w2 = orch._make_worker()
    w3 = orch._make_worker()
    assert isinstance(w1, SSHWorker)
    assert isinstance(w2, SSHWorker)
    assert w1._host == "user@host1"
    assert w2._host == "user@host2"
    assert w3._host == "user@host1"  # round-robin wraps


def test_setup_logging_uses_rich_handler_when_console_provided():
    import logging
    from rich.console import Console
    from rich.logging import RichHandler
    from scale.main import _setup_logging

    console = Console()
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        _setup_logging("INFO", console=console)
        rich_handlers = [h for h in root.handlers if isinstance(h, RichHandler)]
        assert len(rich_handlers) >= 1
        assert rich_handlers[0].console is console
    finally:
        root.handlers = original_handlers


def test_version_command(capsys):
    import sys
    from unittest.mock import patch as mpatch
    with mpatch.object(sys, "argv", ["scale", "version"]):
        from scale.main import main
        main()
    captured = capsys.readouterr()
    assert captured.out.startswith("scale ")


# ---------------------------------------------------------------------------
# Startup cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_cleanup_removes_terminal_workspaces():
    tracker = AsyncMock()
    issue = _issue()
    tracker.fetch_terminal_issues.return_value = [issue]

    orch = Orchestrator(_config(), tracker)
    with patch.object(orch._workspace, "remove", AsyncMock()) as mock_remove:
        await orch._startup_cleanup()

    mock_remove.assert_called_once_with(issue, hooks_enabled=False)


@pytest.mark.asyncio
async def test_startup_cleanup_handles_tracker_error():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.side_effect = RuntimeError("network error")

    orch = Orchestrator(_config(), tracker)
    await orch._startup_cleanup()  # must not raise


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_cancels_stalled_session():
    tracker = AsyncMock()
    issue = _issue()
    tracker.fetch_issues_by_numbers.return_value = [issue]

    orch = Orchestrator(_config(), tracker)
    task = asyncio.create_task(asyncio.sleep(100))
    session = LiveSession(issue=issue, task=task)
    session.last_event_at = datetime.now(tz=timezone.utc) - timedelta(seconds=400)
    orch._state.running[issue.id] = session
    orch._state.claimed.add(issue.id)

    await orch._reconcile()
    await asyncio.sleep(0)

    assert task.cancelled()
    assert any(e.error == "stall timeout" for e in orch._state.retry_queue)


@pytest.mark.asyncio
async def test_reconcile_cancels_terminal_issue():
    tracker = AsyncMock()
    issue = _issue()
    terminal = _issue(state="terminal")
    tracker.fetch_issues_by_numbers.return_value = [terminal]

    orch = Orchestrator(_config(), tracker)
    task = asyncio.create_task(asyncio.sleep(100))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)

    with patch.object(orch._workspace, "remove", AsyncMock()):
        await orch._reconcile()
        await asyncio.sleep(0)

    assert task.cancelled()


@pytest.mark.asyncio
async def test_reconcile_handles_tracker_error():
    tracker = AsyncMock()
    issue = _issue()
    tracker.fetch_issues_by_numbers.side_effect = RuntimeError("network error")

    orch = Orchestrator(_config(), tracker)
    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)

    await orch._reconcile()  # must not raise
    assert issue.id in orch._state.running  # worker kept running


# ---------------------------------------------------------------------------
# Tick — candidate fetch failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_handles_candidate_fetch_failure():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []
    tracker.fetch_candidate_issues.side_effect = RuntimeError("API down")

    orch = Orchestrator(_config(), tracker)
    await orch._tick()  # must not raise
    assert len(orch._state.running) == 0


# ---------------------------------------------------------------------------
# Fire retries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fire_retries_dispatches_due_entry():
    tracker = AsyncMock()
    issue = _issue()
    tracker.fetch_issues_by_numbers.return_value = [issue]

    orch = Orchestrator(_config(), tracker)
    orch._state.claimed.add(issue.id)
    orch._state.retry_queue.append(RetryEntry(
        issue=issue,
        attempt=1,
        due_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1),
        error="prev failure",
    ))

    dispatched: list[str] = []

    async def _noop(iss, attempt):
        dispatched.append(iss.id)

    with patch.object(orch, "_run_worker", side_effect=_noop):
        await orch._fire_retries()

    assert issue.id in dispatched or issue.id in orch._state.running


@pytest.mark.asyncio
async def test_fire_retries_drops_inactive_issue():
    tracker = AsyncMock()
    issue = _issue()
    tracker.fetch_issues_by_numbers.return_value = [_issue(state="terminal")]

    orch = Orchestrator(_config(), tracker)
    orch._state.claimed.add(issue.id)
    orch._state.retry_queue.append(RetryEntry(
        issue=issue,
        attempt=1,
        due_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1),
        error="prev failure",
    ))

    await orch._fire_retries()

    assert issue.id not in orch._state.claimed
    assert len(orch._state.retry_queue) == 0


@pytest.mark.asyncio
async def test_fire_retries_reschedules_when_at_capacity():
    tracker = AsyncMock()
    issue = _issue()
    tracker.fetch_issues_by_numbers.return_value = [issue]

    orch = Orchestrator(_config(max_concurrent_agents=1), tracker)
    orch._state.claimed.add(issue.id)

    # Fill the one slot with a different issue
    other = _issue(id_="i2", number=2)
    orch._state.running["i2"] = LiveSession(issue=other, task=MagicMock())

    orch._state.retry_queue.append(RetryEntry(
        issue=issue,
        attempt=1,
        due_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1),
        error="prev failure",
    ))

    await orch._fire_retries()

    # Should have rescheduled, not dispatched
    assert issue.id not in orch._state.running
    assert any(e.issue.id == issue.id for e in orch._state.retry_queue)


@pytest.mark.asyncio
async def test_on_event_accumulates_tokens_across_multiple_assistant_events():
    """on_event must += tokens so each turn's usage is summed, not overwritten."""
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        if on_event:
            on_event({"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}})
            on_event({"type": "assistant", "message": {"usage": {"input_tokens": 200, "output_tokens": 80}}})

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker:
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    assert orch._state.token_totals.input_tokens == 300
    assert orch._state.token_totals.output_tokens == 130


@pytest.mark.asyncio
async def test_on_event_increments_turn_count_on_assistant():
    """on_event must increment turn_count for each assistant event received."""
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()
    captured_turn_counts: list[int] = []

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        if on_event:
            on_event({"type": "assistant"})
            s = orch._state.running.get(issue.id)
            if s:
                captured_turn_counts.append(s.turn_count)
            on_event({"type": "assistant"})
            s = orch._state.running.get(issue.id)
            if s:
                captured_turn_counts.append(s.turn_count)

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker:
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    assert captured_turn_counts == [1, 2]


@pytest.mark.asyncio
async def test_on_event_accumulates_tokens_from_assistant_event_including_cache():
    """on_event reads usage from assistant events, summing all cache token variants."""
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()
    captured_tokens: list[tuple[int, int]] = []

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        if on_event:
            on_event({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 3,
                        "cache_creation_input_tokens": 11072,
                        "cache_read_input_tokens": 15912,
                        "output_tokens": 8,
                    }
                },
            })
            s = orch._state.running.get(issue.id)
            if s:
                captured_tokens.append((s.tokens.input_tokens, s.tokens.output_tokens))

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker:
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    assert captured_tokens == [(15915, 11080)]  # in: 3 + 15912 (cache_read); out: 8 + 11072 (cache_creation/thinking)


def test_triage_subcommand_help(capsys):
    import sys
    from unittest.mock import patch as mpatch
    with mpatch.object(sys, "argv", ["scale", "triage", "--help"]):
        from scale.main import main
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "triage" in captured.out or "triage" in captured.err


# ---------------------------------------------------------------------------
# Planner integration
# ---------------------------------------------------------------------------

def _config_with_planner(**kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        polling=PollingConfig(interval_ms=0),
        prompt_template="Work on {{ issue.title }}.",
        planner=PlannerConfig(**kwargs),
    )

def _plan_issue(number=10) -> Issue:
    return Issue(
        id=f"plan{number}", identifier=f"o/r#{number}", number=number,
        title="Big concept", description="", state="active",
        labels=["scale:plan"], branch_name=f"symphony/{number}-big-concept",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


@pytest.mark.asyncio
async def test_tick_dispatches_plan_issues_to_planner():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []
    tracker.fetch_issues_by_label.return_value = [_plan_issue()]

    with patch("scale.orchestrator.core.PlannerRunner"):
        orch = Orchestrator(_config_with_planner(), tracker)
        with patch.object(orch, "_run_planner", AsyncMock()) as mock_run_planner:
            await orch._tick()
            await asyncio.sleep(0)  # let spawned tasks run

    mock_run_planner.assert_called_once()
    assert mock_run_planner.call_args[0][0].number == 10


@pytest.mark.asyncio
async def test_tick_skips_planner_when_not_configured():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    await orch._tick()
    tracker.fetch_issues_by_label.assert_not_called()


@pytest.mark.asyncio
async def test_watch_planned_closes_parent_when_all_children_done():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []

    parent = _issue(id_="parent1", number=42, state="active")
    parent.labels = ["scale:planned"]
    child1 = _issue(id_="c1", number=51, state="terminal")
    child2 = _issue(id_="c2", number=52, state="terminal")

    tracker.fetch_issues_by_label.return_value = [parent]
    tracker.fetch_issues_by_numbers.return_value = [child1, child2]

    with patch("scale.orchestrator.core.PlannerRunner") as MockRunner:
        instance = MockRunner.return_value
        instance.get_child_numbers = AsyncMock(return_value=[51, 52])
        instance.plan_issue = AsyncMock()
        orch = Orchestrator(_config_with_planner(), tracker)
        with patch.object(orch, "_gh_add_labels", AsyncMock()) as mock_add, \
             patch.object(orch, "_gh_remove_label", AsyncMock()) as mock_remove:
            await orch._watch_planned_tick()

    mock_add.assert_called_once_with(42, ["scale:done"])
    mock_remove.assert_called_once_with(42, "scale:planned")


# ---------------------------------------------------------------------------
# Triage integration
# ---------------------------------------------------------------------------

def _config_with_triage(**kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        polling=PollingConfig(interval_ms=0),
        prompt_template="Work on {{ issue.title }}.",
        triage=TriageConfig(**kwargs),
    )


def _untriaged_issue(number=20, labels: list[str] | None = None) -> Issue:
    return Issue(
        id=f"ut{number}", identifier=f"o/r#{number}", number=number,
        title="Untriaged feature", description="", state="active",
        labels=labels if labels is not None else [],
        branch_name=f"symphony/{number}-untriaged-feature",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


@pytest.mark.asyncio
async def test_tick_dispatches_issues_with_triage_label_to_triage_runner():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    issue = _untriaged_issue(labels=["scale:triage"])

    with patch("scale.orchestrator.core.TriageRunner"):
        orch = Orchestrator(_config_with_triage(), tracker)
        orch._github = AsyncMock()
        orch._github.fetch_open_issues.return_value = [issue]
        with patch.object(orch, "_run_triage", AsyncMock()) as mock_run_triage:
            await orch._tick()
            await asyncio.sleep(0)

    mock_run_triage.assert_called_once()
    assert mock_run_triage.call_args[0][0].number == 20


@pytest.mark.asyncio
async def test_tick_ignores_issues_without_triage_label():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    unlabeled = _untriaged_issue(number=20, labels=[])
    other_label = _untriaged_issue(number=21, labels=["bug"])

    with patch("scale.orchestrator.core.TriageRunner"):
        orch = Orchestrator(_config_with_triage(), tracker)
        orch._github = AsyncMock()
        orch._github.fetch_open_issues.return_value = [unlabeled, other_label]
        with patch.object(orch, "_run_triage", AsyncMock()) as mock_run_triage:
            await orch._tick()
            await asyncio.sleep(0)

    mock_run_triage.assert_not_called()


@pytest.mark.asyncio
async def test_tick_skips_triage_when_not_configured():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    orch._github = AsyncMock()
    await orch._tick()
    orch._github.fetch_open_issues.assert_not_called()


@pytest.mark.asyncio
async def test_tick_skips_issues_that_have_triage_exclusion_labels():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    triaged_issue = _untriaged_issue(number=20, labels=["scale:triage", "scale:triaged"])
    ready_issue = _untriaged_issue(number=21, labels=["scale:triage", "scale:ready"])
    needs_detail_issue = _untriaged_issue(number=22, labels=["scale:triage", "scale:needs-detail"])

    with patch("scale.orchestrator.core.TriageRunner"):
        orch = Orchestrator(_config_with_triage(), tracker)
        orch._github = AsyncMock()
        orch._github.fetch_open_issues.return_value = [
            triaged_issue, ready_issue, needs_detail_issue
        ]
        with patch.object(orch, "_run_triage", AsyncMock()) as mock_run_triage:
            await orch._tick()
            await asyncio.sleep(0)

    mock_run_triage.assert_not_called()


@pytest.mark.asyncio
async def test_tick_does_not_retriage_claimed_issues():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    issue = _untriaged_issue(labels=["scale:triage"])

    with patch("scale.orchestrator.core.TriageRunner"):
        orch = Orchestrator(_config_with_triage(), tracker)
        orch._state.claimed.add(issue.id)
        orch._github = AsyncMock()
        orch._github.fetch_open_issues.return_value = [issue]
        with patch.object(orch, "_run_triage", AsyncMock()) as mock_run_triage:
            await orch._tick()
            await asyncio.sleep(0)

    mock_run_triage.assert_not_called()


@pytest.mark.asyncio
async def test_run_triage_releases_claim_on_success():
    tracker = AsyncMock()
    issue = _untriaged_issue()

    with patch("scale.orchestrator.core.TriageRunner"):
        orch = Orchestrator(_config_with_triage(), tracker)
        orch._state.claimed.add(issue.id)
        orch._triage_runner = AsyncMock()
        orch._triage_runner.triage_issue = AsyncMock()
        await orch._run_triage(issue)

    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_run_triage_releases_claim_on_error():
    tracker = AsyncMock()
    issue = _untriaged_issue()

    with patch("scale.orchestrator.core.TriageRunner"):
        orch = Orchestrator(_config_with_triage(), tracker)
        orch._state.claimed.add(issue.id)
        orch._triage_runner = AsyncMock()
        orch._triage_runner.triage_issue = AsyncMock(side_effect=RuntimeError("ai error"))
        await orch._run_triage(issue)

    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_tick_triage_fetch_failure_does_not_crash():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    with patch("scale.orchestrator.core.TriageRunner"):
        orch = Orchestrator(_config_with_triage(), tracker)
        orch._github = AsyncMock()
        orch._github.fetch_open_issues.side_effect = RuntimeError("network down")
        await orch._tick()  # must not raise


# ---------------------------------------------------------------------------
# Finishing state / CompletedSession
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_worker_sets_finishing_on_success():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        pass

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", AsyncMock()), \
         patch.object(orch._workspace, "remove", AsyncMock()):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    assert issue.id in orch._state.running
    assert orch._state.running[issue.id].finishing is True
    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_completed_session_appended_on_success():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        if on_event:
            on_event({"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}})

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", AsyncMock()), \
         patch.object(orch._workspace, "remove", AsyncMock()):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    await orch._flush_finishing()
    assert len(orch._state.completed) == 1
    cs = orch._state.completed[0]
    assert isinstance(cs, CompletedSession)
    assert cs.issue.id == issue.id
    assert cs.tokens.input_tokens == 100
    assert cs.tokens.output_tokens == 50


@pytest.mark.asyncio
async def test_total_completed_incremented_on_success():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        pass

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", AsyncMock()), \
         patch.object(orch._workspace, "remove", AsyncMock()):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    await orch._flush_finishing()
    assert orch._state.total_completed == 1


@pytest.mark.asyncio
async def test_flush_finishing_moves_session_to_completed():
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker)
    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)
    session.finishing = True
    session.tokens.input_tokens = 42
    session.tokens.output_tokens = 17
    orch._state.running[issue.id] = session

    await orch._flush_finishing()

    assert issue.id not in orch._state.running
    assert len(orch._state.completed) == 1
    assert orch._state.completed[0].issue.id == issue.id
    assert orch._state.token_totals.input_tokens == 42
    assert orch._state.token_totals.output_tokens == 17
    assert orch._state.total_completed == 1


@pytest.mark.asyncio
async def test_flush_finishing_skips_active_sessions():
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker)
    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)
    session.finishing = False
    orch._state.running[issue.id] = session

    await orch._flush_finishing()

    assert issue.id in orch._state.running
    assert len(orch._state.completed) == 0


@pytest.mark.asyncio
async def test_finishing_sessions_flushed_on_next_tick():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)
    session.finishing = True
    session.tokens.input_tokens = 10
    session.tokens.output_tokens = 5
    orch._state.running[issue.id] = session

    await orch._tick()

    assert issue.id not in orch._state.running
    assert any(cs.issue.id == issue.id for cs in orch._state.completed)
    assert orch._state.token_totals.input_tokens == 10


@pytest.mark.asyncio
async def test_expire_completed_removes_old_entries():
    tracker = AsyncMock()
    orch = Orchestrator(_config(completed_display_s=300), tracker)

    old_cs = CompletedSession(
        issue=_issue(),
        turn_count=1,
        tokens=TokenTotals(input_tokens=10, output_tokens=5),
        completed_at=datetime.now(tz=timezone.utc) - timedelta(seconds=400),
    )
    orch._state.completed.append(old_cs)

    await orch._expire_completed()

    assert len(orch._state.completed) == 0


@pytest.mark.asyncio
async def test_expire_completed_keeps_recent_entries():
    tracker = AsyncMock()
    orch = Orchestrator(_config(completed_display_s=300), tracker)

    recent_cs = CompletedSession(
        issue=_issue(),
        turn_count=2,
        tokens=TokenTotals(input_tokens=20, output_tokens=10),
        completed_at=datetime.now(tz=timezone.utc) - timedelta(seconds=60),
    )
    orch._state.completed.append(recent_cs)

    await orch._expire_completed()

    assert len(orch._state.completed) == 1
    assert orch._state.completed[0] is recent_cs


@pytest.mark.asyncio
async def test_tick_emits_summary_log(caplog):
    tracker = AsyncMock()
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)

    with caplog.at_level(logging.INFO, logger="scale.orchestrator.core"):
        await orch._tick()

    tick_lines = [r.message for r in caplog.records if r.message.startswith("tick:")]
    assert len(tick_lines) == 1
    assert "running=" in tick_lines[0]
    assert "retries=" in tick_lines[0]
    assert "completed=" in tick_lines[0]


@pytest.mark.asyncio
async def test_tick_summary_counts_are_accurate(caplog):
    tracker = AsyncMock()
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.retry_queue.append(
        RetryEntry(
            issue=_issue("i2", 2),
            attempt=1,
            due_at=datetime.now(tz=timezone.utc),
            error="test",
        )
    )
    orch._state.total_completed = 3

    with caplog.at_level(logging.INFO, logger="scale.orchestrator.core"):
        await orch._tick()

    tick_lines = [r.message for r in caplog.records if r.message.startswith("tick:")]
    assert len(tick_lines) >= 1
    last = tick_lines[-1]
    assert "completed=3" in last


# ---------------------------------------------------------------------------
# _record_stats
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_stats_posts_github_comment(tmp_path):
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker)
    orch._github = AsyncMock()
    orch._github.post_comment = AsyncMock()

    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)
    session.turn_count = 5
    session.tokens.input_tokens = 1000
    session.tokens.output_tokens = 500

    with patch("scale.orchestrator.core.Path", return_value=tmp_path / "stats.jsonl"):
        await orch._record_stats(issue, session, success=True, attempt=None)

    orch._github.post_comment.assert_called_once()
    call_args = orch._github.post_comment.call_args
    assert call_args[0][0] == issue.number
    body = call_args[0][1]
    assert "<!-- scale-stats" in body
    assert '"success": true' in body
    assert "Scale run complete" in body
    assert "Turns:" in body


@pytest.mark.asyncio
async def test_record_stats_writes_to_stats_jsonl(tmp_path):
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker)
    orch._github = AsyncMock()
    orch._github.post_comment = AsyncMock()

    issue = _issue(number=42)
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)
    session.turn_count = 7
    session.tokens.input_tokens = 2000
    session.tokens.output_tokens = 800

    stats_file = tmp_path / "stats.jsonl"

    with patch("scale.orchestrator.core.Path", return_value=stats_file):
        await orch._record_stats(issue, session, success=True, attempt=None)

    assert stats_file.exists()
    line = stats_file.read_text().strip()
    record = json.loads(line)
    assert record["issue"] == 42
    assert record["turns"] == 7
    assert record["input_tokens"] == 2000
    assert record["output_tokens"] == 800
    assert record["success"] is True
    assert "timestamp" in record


@pytest.mark.asyncio
async def test_record_stats_includes_failure_flag(tmp_path):
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker)
    orch._github = AsyncMock()
    orch._github.post_comment = AsyncMock()

    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)

    stats_file = tmp_path / "stats.jsonl"

    with patch("scale.orchestrator.core.Path", return_value=stats_file):
        await orch._record_stats(issue, session, success=False, attempt=1)

    record = json.loads(stats_file.read_text().strip())
    assert record["success"] is False
    assert record["attempt"] == 2

    body = orch._github.post_comment.call_args[0][1]
    assert '"success": false' in body


@pytest.mark.asyncio
async def test_record_stats_called_on_worker_success(tmp_path):
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        pass

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    stats_file = tmp_path / "stats.jsonl"

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", AsyncMock()), \
         patch.object(orch._github, "post_comment", AsyncMock()), \
         patch.object(orch._workspace, "remove", AsyncMock()), \
         patch("scale.orchestrator.core.Path", return_value=stats_file):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    assert stats_file.exists()
    record = json.loads(stats_file.read_text().strip())
    assert record["success"] is True
    assert record["issue"] == issue.number


@pytest.mark.asyncio
async def test_record_stats_called_on_worker_failure(tmp_path):
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run_fail(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        raise RuntimeError("agent crashed")

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    stats_file = tmp_path / "stats.jsonl"

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "post_comment", AsyncMock()), \
         patch("scale.orchestrator.core.Path", return_value=stats_file):
        mock_w = MagicMock()
        mock_w.run = _mock_run_fail
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    assert stats_file.exists()
    record = json.loads(stats_file.read_text().strip())
    assert record["success"] is False


@pytest.mark.asyncio
async def test_record_stats_github_failure_does_not_crash(tmp_path):
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker)
    orch._github = AsyncMock()
    orch._github.post_comment = AsyncMock(side_effect=RuntimeError("network error"))

    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)
    stats_file = tmp_path / "stats.jsonl"

    with patch("scale.orchestrator.core.Path", return_value=stats_file):
        await orch._record_stats(issue, session, success=True, attempt=None)  # must not raise

    assert stats_file.exists()


# ---------------------------------------------------------------------------
# Attempt summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_previous_attempt_summary_returns_latest_summary_comment():
    orch = Orchestrator(_config(), AsyncMock())
    issue = _issue()
    orch._github = AsyncMock()
    orch._github.fetch_issue_comments = AsyncMock(return_value=[
        {"body": "Some other comment"},
        {"body": "<!-- scale-attempt-summary -->\n\n## Scale attempt 1 summary\nFiles: foo.py"},
    ])

    result = await orch._fetch_previous_attempt_summary(issue)

    assert result is not None
    assert "scale-attempt-summary" in result
    assert "foo.py" in result


@pytest.mark.asyncio
async def test_fetch_previous_attempt_summary_returns_none_when_no_summary():
    orch = Orchestrator(_config(), AsyncMock())
    issue = _issue()
    orch._github = AsyncMock()
    orch._github.fetch_issue_comments = AsyncMock(return_value=[
        {"body": "Just a regular comment"},
    ])

    result = await orch._fetch_previous_attempt_summary(issue)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_previous_attempt_summary_returns_most_recent():
    orch = Orchestrator(_config(), AsyncMock())
    issue = _issue()
    orch._github = AsyncMock()
    orch._github.fetch_issue_comments = AsyncMock(return_value=[
        {"body": "<!-- scale-attempt-summary -->\nAttempt 1: modified alpha.py"},
        {"body": "<!-- scale-attempt-summary -->\nAttempt 2: modified beta.py"},
    ])

    result = await orch._fetch_previous_attempt_summary(issue)

    assert result is not None
    assert "beta.py" in result


@pytest.mark.asyncio
async def test_fetch_previous_attempt_summary_handles_github_error():
    orch = Orchestrator(_config(), AsyncMock())
    issue = _issue()
    orch._github = AsyncMock()
    orch._github.fetch_issue_comments = AsyncMock(side_effect=RuntimeError("network"))

    result = await orch._fetch_previous_attempt_summary(issue)

    assert result is None


@pytest.mark.asyncio
async def test_post_attempt_summary_posts_comment_with_marker():
    orch = Orchestrator(_config(), AsyncMock())
    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)
    session.turn_count = 3

    orch._github = AsyncMock()
    orch._github.post_comment = AsyncMock()

    with patch.object(orch, "_collect_workspace_state", AsyncMock(return_value={
        "modified_files": ["scale/foo.py"],
        "new_files": [],
        "commits": ["abc1234 Add feature"],
    })):
        await orch._post_attempt_summary(issue, session, attempt=None)

    orch._github.post_comment.assert_called_once()
    body = orch._github.post_comment.call_args[0][1]
    assert "<!-- scale-attempt-summary -->" in body
    assert "scale/foo.py" in body
    assert "abc1234" in body


@pytest.mark.asyncio
async def test_post_attempt_summary_handles_missing_workspace():
    orch = Orchestrator(_config(), AsyncMock())
    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)

    orch._github = AsyncMock()
    orch._github.post_comment = AsyncMock()

    with patch.object(orch, "_collect_workspace_state", AsyncMock(return_value={
        "modified_files": [],
        "new_files": [],
        "commits": [],
    })):
        await orch._post_attempt_summary(issue, session, attempt=None)

    orch._github.post_comment.assert_called_once()
    body = orch._github.post_comment.call_args[0][1]
    assert "<!-- scale-attempt-summary -->" in body


@pytest.mark.asyncio
async def test_post_attempt_summary_github_failure_does_not_raise():
    orch = Orchestrator(_config(), AsyncMock())
    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    session = LiveSession(issue=issue, task=task)

    orch._github = AsyncMock()
    orch._github.post_comment = AsyncMock(side_effect=RuntimeError("network"))

    with patch.object(orch, "_collect_workspace_state", AsyncMock(return_value={
        "modified_files": [],
        "new_files": [],
        "commits": [],
    })):
        await orch._post_attempt_summary(issue, session, attempt=None)  # must not raise


@pytest.mark.asyncio
async def test_run_worker_passes_previous_summary_on_retry(tmp_path):
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    received: list[dict] = []

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        received.append({"previous_attempt_summary": previous_attempt_summary})

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    stats_file = tmp_path / "stats.jsonl"

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", AsyncMock()), \
         patch.object(orch._github, "post_comment", AsyncMock()), \
         patch.object(orch._workspace, "remove", AsyncMock()), \
         patch.object(orch, "_fetch_previous_attempt_summary",
                      AsyncMock(return_value="Previous: foo.py modified")), \
         patch("scale.orchestrator.core.Path", return_value=stats_file):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=2)

    assert len(received) == 1
    assert received[0]["previous_attempt_summary"] == "Previous: foo.py modified"


@pytest.mark.asyncio
async def test_run_worker_no_previous_summary_on_first_attempt(tmp_path):
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    received: list[dict] = []

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        received.append({"previous_attempt_summary": previous_attempt_summary})

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    stats_file = tmp_path / "stats.jsonl"

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", AsyncMock()), \
         patch.object(orch._github, "post_comment", AsyncMock()), \
         patch.object(orch._workspace, "remove", AsyncMock()), \
         patch("scale.orchestrator.core.Path", return_value=stats_file):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    assert len(received) == 1
    assert received[0]["previous_attempt_summary"] is None


@pytest.mark.asyncio
async def test_run_worker_posts_attempt_summary_on_failure(tmp_path):
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run_fail(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        raise RuntimeError("agent crashed")

    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    post_summary_mock = AsyncMock()
    stats_file = tmp_path / "stats.jsonl"

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch, "_post_attempt_summary", post_summary_mock), \
         patch.object(orch._github, "post_comment", AsyncMock()), \
         patch("scale.orchestrator.core.Path", return_value=stats_file):
        mock_w = MagicMock()
        mock_w.run = _mock_run_fail
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    post_summary_mock.assert_called_once()


# ---------------------------------------------------------------------------
# auto_merge config
# ---------------------------------------------------------------------------

def test_agent_config_auto_merge_defaults_false():
    cfg = AgentConfig()
    assert cfg.auto_merge is False


def test_agent_config_auto_merge_can_be_enabled():
    cfg = AgentConfig(auto_merge=True)
    assert cfg.auto_merge is True


# ---------------------------------------------------------------------------
# _try_auto_merge
# ---------------------------------------------------------------------------

def _config_with_review(**agent_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        polling=PollingConfig(interval_ms=0),
        agent=AgentConfig(**agent_kwargs),
        review=ReviewConfig(),
        prompt_template="Work on {{ issue.title }}.",
    )


@pytest.mark.asyncio
async def test_try_auto_merge_merges_when_checks_pass():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(auto_merge=True), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_checks = AsyncMock(return_value=[
        {"status": "completed", "conclusion": "success"}
    ])
    orch._github.merge_pr = AsyncMock()

    issue = _issue()
    await orch._try_auto_merge(issue, pr_number=42)

    orch._github.merge_pr.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_try_auto_merge_skips_merge_when_checks_fail():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(auto_merge=True), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_checks = AsyncMock(return_value=[
        {"status": "completed", "conclusion": "failure"}
    ])
    orch._github.merge_pr = AsyncMock()

    issue = _issue()
    await orch._try_auto_merge(issue, pr_number=42)

    orch._github.merge_pr.assert_not_called()


@pytest.mark.asyncio
async def test_try_auto_merge_merges_when_no_checks():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(auto_merge=True), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_checks = AsyncMock(return_value=[])
    orch._github.merge_pr = AsyncMock()

    issue = _issue()
    await orch._try_auto_merge(issue, pr_number=42)

    orch._github.merge_pr.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_try_auto_merge_waits_for_in_progress_checks():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(auto_merge=True), tracker)
    orch._github = AsyncMock()
    call_count = 0

    async def _checks(_pr_number):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [{"status": "in_progress", "conclusion": None}]
        return [{"status": "completed", "conclusion": "success"}]

    orch._github.fetch_pr_checks = _checks
    orch._github.merge_pr = AsyncMock()

    issue = _issue()
    with patch("asyncio.sleep", AsyncMock()):
        await orch._try_auto_merge(issue, pr_number=42)

    orch._github.merge_pr.assert_called_once_with(42)
    assert call_count == 2


# ---------------------------------------------------------------------------
# _run_reviewer: auto_merge integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_reviewer_approve_adds_merge_label():
    from scale.agent.claude import TurnResult
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(auto_merge=True), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 10, "html_url": "http://pr"})
    orch._github.fetch_pr_diff = AsyncMock(return_value="diff")
    orch._github.add_labels = AsyncMock()
    orch._github.remove_label = AsyncMock()
    orch._github.post_comment = AsyncMock()

    issue = _issue()
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.ReviewWorker") as MockRW:
        mock_rw = MagicMock()
        mock_rw.run = AsyncMock(return_value=TurnResult(
            success=True, usage=None, message="LGTM.\nVERDICT: APPROVE"
        ))
        MockRW.return_value = mock_rw
        await orch._run_reviewer(issue)

    orch._github.add_labels.assert_called_once_with(issue.number, ["scale:merge"])
    orch._github.remove_label.assert_called_once_with(issue.number, "scale:pr-open")


@pytest.mark.asyncio
async def test_run_reviewer_request_changes_adds_needs_revision():
    from scale.agent.claude import TurnResult
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(auto_merge=False), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 10, "html_url": "http://pr"})
    orch._github.fetch_pr_diff = AsyncMock(return_value="diff")
    orch._github.add_labels = AsyncMock()
    orch._github.remove_label = AsyncMock()
    orch._github.post_comment = AsyncMock()

    issue = _issue()
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.ReviewWorker") as MockRW:
        mock_rw = MagicMock()
        mock_rw.run = AsyncMock(return_value=TurnResult(
            success=True, usage=None,
            message="Missing tests.\nVERDICT: REQUEST_CHANGES: tests are missing"
        ))
        MockRW.return_value = mock_rw
        await orch._run_reviewer(issue)

    orch._github.add_labels.assert_called_once_with(issue.number, ["scale:needs-revision"])
    orch._github.remove_label.assert_called_once_with(issue.number, "scale:pr-open")


# ---------------------------------------------------------------------------
# _tick: supervised issues skipped by reviewer dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_skips_supervised_issues_for_reviewer():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    supervised_issue = _issue(id_="sup1", number=5)
    supervised_issue.labels = ["scale:supervised", "scale:pr-open"]

    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_issues_by_label = AsyncMock(return_value=[supervised_issue])
    orch._github.fetch_open_issues = AsyncMock(return_value=[])

    with patch.object(orch, "_run_reviewer", AsyncMock()) as mock_reviewer:
        await orch._tick()
        await asyncio.sleep(0)

    mock_reviewer.assert_not_called()


@pytest.mark.asyncio
async def test_tick_dispatches_non_supervised_for_reviewer():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    normal_issue = _issue(id_="n1", number=6)
    normal_issue.labels = ["scale:pr-open"]

    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_issues_by_label = AsyncMock(return_value=[normal_issue])
    orch._github.fetch_open_issues = AsyncMock(return_value=[])

    with patch.object(orch, "_run_reviewer", AsyncMock()) as mock_reviewer:
        await orch._tick()
        await asyncio.sleep(0)

    mock_reviewer.assert_called_once()


# ---------------------------------------------------------------------------
# _watch_merge_queue_tick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watch_merge_queue_tick_merges_issues_with_merge_label():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()

    merge_issue = _issue(id_="m1", number=9)
    merge_issue.labels = ["scale:pr-open", "scale:merge"]

    orch._github.fetch_issues_by_label = AsyncMock(return_value=[merge_issue])

    with patch.object(orch, "_merge_issue", AsyncMock()) as mock_merge:
        await orch._watch_merge_queue_tick()
        await asyncio.sleep(0)

    mock_merge.assert_called_once_with(merge_issue)


@pytest.mark.asyncio
async def test_watch_merge_queue_tick_ignores_issues_without_merge_label():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()

    pr_open_only = _issue(id_="p1", number=8)
    pr_open_only.labels = ["scale:pr-open"]

    orch._github.fetch_issues_by_label = AsyncMock(return_value=[pr_open_only])

    with patch.object(orch, "_merge_issue", AsyncMock()) as mock_merge:
        await orch._watch_merge_queue_tick()

    mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_watch_merge_queue_tick_skips_claimed_issues():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()

    merge_issue = _issue(id_="m2", number=11)
    merge_issue.labels = ["scale:pr-open", "scale:merge"]
    orch._state.claimed.add(merge_issue.id)

    orch._github.fetch_issues_by_label = AsyncMock(return_value=[merge_issue])

    with patch.object(orch, "_merge_issue", AsyncMock()) as mock_merge:
        await orch._watch_merge_queue_tick()

    mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_watch_merge_queue_tick_skips_when_no_review_config():
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker)
    orch._github = AsyncMock()

    await orch._watch_merge_queue_tick()

    orch._github.fetch_issues_by_label.assert_not_called()


# ---------------------------------------------------------------------------
# _merge_issue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_issue_merges_pr_and_applies_terminal_label():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 55})
    orch._github.merge_pr = AsyncMock()
    orch._github.add_labels = AsyncMock()
    orch._github.remove_label = AsyncMock()

    issue = _issue()
    orch._state.claimed.add(issue.id)
    await orch._merge_issue(issue)

    orch._github.merge_pr.assert_called_once_with(55)
    orch._github.add_labels.assert_called_once_with(issue.number, ["scale:done"])
    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_merge_issue_removes_pr_open_and_merge_labels():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 55})
    orch._github.merge_pr = AsyncMock()
    orch._github.add_labels = AsyncMock()
    orch._github.remove_label = AsyncMock()

    issue = _issue()
    orch._state.claimed.add(issue.id)
    await orch._merge_issue(issue)

    removed = [call.args[1] for call in orch._github.remove_label.call_args_list]
    assert "scale:pr-open" in removed
    assert "scale:merge" in removed


@pytest.mark.asyncio
async def test_merge_issue_skips_when_no_pr_found():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value=None)
    orch._github.merge_pr = AsyncMock()

    issue = _issue()
    orch._state.claimed.add(issue.id)
    await orch._merge_issue(issue)

    orch._github.merge_pr.assert_not_called()
    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_merge_issue_releases_claim_on_error():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 55})
    orch._github.merge_pr = AsyncMock(side_effect=RuntimeError("merge conflict"))
    orch._github.add_labels = AsyncMock()
    orch._github.remove_label = AsyncMock()

    issue = _issue()
    orch._state.claimed.add(issue.id)
    await orch._merge_issue(issue)  # must not raise

    assert issue.id not in orch._state.claimed


# ---------------------------------------------------------------------------
# Secondary session tracking (review / feedback workers)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_reviewer_sets_secondary_during_run():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 10, "html_url": "http://pr"})
    orch._github.fetch_pr_diff = AsyncMock(return_value="diff")
    orch._github.add_labels = AsyncMock()
    orch._github.remove_label = AsyncMock()

    issue = _issue()
    orch._state.claimed.add(issue.id)
    secondary_during: list[str] = []

    async def _mock_run(*args, **kwargs):
        if issue.id in orch._state.secondary:
            secondary_during.append(orch._state.secondary[issue.id].kind)

    with patch("scale.orchestrator.core.ReviewWorker") as MockRW:
        mock_rw = MagicMock()
        mock_rw.run = _mock_run
        MockRW.return_value = mock_rw
        await orch._run_reviewer(issue)

    assert secondary_during == ["review"]
    assert issue.id not in orch._state.secondary


@pytest.mark.asyncio
async def test_run_reviewer_clears_secondary_on_error():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 10, "html_url": "http://pr"})
    orch._github.fetch_pr_diff = AsyncMock(return_value="diff")
    orch._github.add_labels = AsyncMock()
    orch._github.remove_label = AsyncMock()

    issue = _issue()
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.ReviewWorker") as MockRW:
        mock_rw = MagicMock()
        mock_rw.run = AsyncMock(side_effect=RuntimeError("review failed"))
        MockRW.return_value = mock_rw
        await orch._run_reviewer(issue)

    assert issue.id not in orch._state.secondary


@pytest.mark.asyncio
async def test_run_feedback_worker_sets_secondary_during_run():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 10})
    orch._github.fetch_pr_diff = AsyncMock(return_value="diff")

    issue = _issue()
    orch._state.claimed.add(issue.id)
    secondary_during: list[str] = []

    async def _mock_run(*args, **kwargs):
        if issue.id in orch._state.secondary:
            secondary_during.append(orch._state.secondary[issue.id].kind)

    with patch("scale.orchestrator.core.FeedbackWorker") as MockFW:
        mock_fw = MagicMock()
        mock_fw.run = _mock_run
        MockFW.return_value = mock_fw
        await orch._run_feedback_worker(issue, comments=[{"body": "fix this"}])

    assert secondary_during == ["feedback"]
    assert issue.id not in orch._state.secondary


@pytest.mark.asyncio
async def test_run_feedback_worker_clears_secondary_on_error():
    tracker = AsyncMock()
    orch = Orchestrator(_config_with_review(), tracker)
    orch._github = AsyncMock()
    orch._github.fetch_pr_for_branch = AsyncMock(return_value={"number": 10})
    orch._github.fetch_pr_diff = AsyncMock(return_value="diff")

    issue = _issue()
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.FeedbackWorker") as MockFW:
        mock_fw = MagicMock()
        mock_fw.run = AsyncMock(side_effect=RuntimeError("feedback crashed"))
        MockFW.return_value = mock_fw
        await orch._run_feedback_worker(issue, comments=[{"body": "fix this"}])

    assert issue.id not in orch._state.secondary
