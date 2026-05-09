import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from scale.orchestrator.core import Orchestrator
from scale.orchestrator.state import CompletedSession, LiveSession, RetryEntry, TokenTotals
from scale.config.schema import WorkflowConfig, TrackerConfig, AgentConfig, WorkerConfig, PlannerConfig, TriageConfig
from scale.tracker.models import Issue
from scale.worker.local import LocalWorker
from scale.worker.ssh import SSHWorker

def _config(**agent_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        agent=AgentConfig(**agent_kwargs),
        prompt_template="Work on {{ issue.title }}.",
    )

def _config_ssh(*hosts: str) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
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

    async def _mock_run(iss, cfg, attempt, on_event=None):
        if on_event:
            on_event({"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50}})

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

        async def _mock_run(iss, cfg, attempt, on_event=None, _i=inp_cap, _o=out_cap):
            if on_event:
                on_event({"type": "result", "usage": {"input_tokens": _i, "output_tokens": _o}})

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
async def test_on_event_accumulates_tokens_across_multiple_result_events():
    """on_event must += tokens so each turn's usage is summed, not overwritten."""
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    issue = _issue()

    async def _mock_run(iss, cfg, attempt, on_event=None):
        if on_event:
            on_event({"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50}})
            on_event({"type": "result", "usage": {"input_tokens": 200, "output_tokens": 80}})

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

    async def _mock_run(iss, cfg, attempt, on_event=None):
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
        prompt_template="Work on {{ issue.title }}.",
        planner=PlannerConfig(**kwargs),
    )

def _plan_issue(number=10) -> Issue:
    return Issue(
        id=f"plan{number}", identifier=f"o/r#{number}", number=number,
        title="Big concept", description="", state="active",
        labels=["symphony:plan"], branch_name=f"symphony/{number}-big-concept",
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
    parent.labels = ["symphony:planned"]
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

    mock_add.assert_called_once_with(42, ["symphony:done"])
    mock_remove.assert_called_once_with(42, "symphony:planned")


# ---------------------------------------------------------------------------
# Triage integration
# ---------------------------------------------------------------------------

def _config_with_triage(**kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
        triage=TriageConfig(**kwargs),
    )


def _untriaged_issue(number=20) -> Issue:
    return Issue(
        id=f"ut{number}", identifier=f"o/r#{number}", number=number,
        title="Untriaged feature", description="", state="active",
        labels=[], branch_name=f"symphony/{number}-untriaged-feature",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


@pytest.mark.asyncio
async def test_tick_dispatches_untriaged_issues_to_triage_runner():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    issue = _untriaged_issue()

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
async def test_tick_skips_issues_that_have_triage_labels():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    triaged_issue = _untriaged_issue()
    triaged_issue.labels = ["symphony:triaged"]

    ready_issue = _untriaged_issue(number=21)
    ready_issue.labels = ["symphony:ready"]

    needs_detail_issue = _untriaged_issue(number=22)
    needs_detail_issue.labels = ["symphony:needs-detail"]

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

    issue = _untriaged_issue()

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

    async def _mock_run(iss, cfg, attempt, on_event=None):
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

    async def _mock_run(iss, cfg, attempt, on_event=None):
        if on_event:
            on_event({"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50}})

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

    async def _mock_run(iss, cfg, attempt, on_event=None):
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
