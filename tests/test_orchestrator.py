import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from symphony.orchestrator.core import Orchestrator
from symphony.orchestrator.state import LiveSession, RetryEntry
from symphony.config.schema import WorkflowConfig, TrackerConfig, AgentConfig, WorkerConfig
from symphony.tracker.models import Issue
from symphony.worker.local import LocalWorker
from symphony.worker.ssh import SSHWorker

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

    with patch("symphony.orchestrator.core.LocalWorker") as MockWorker:
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

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

        with patch("symphony.orchestrator.core.LocalWorker") as MockWorker:
            mock_w = MagicMock()
            mock_w.run = _mock_run
            MockWorker.return_value = mock_w
            await orch._run_worker(issue, attempt=None)

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


def test_version_command(capsys):
    import sys
    from unittest.mock import patch as mpatch
    with mpatch.object(sys, "argv", ["symphony", "version"]):
        from symphony.main import main
        main()
    captured = capsys.readouterr()
    assert captured.out.startswith("symphony ")


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


def test_triage_subcommand_help(capsys):
    import sys
    from unittest.mock import patch as mpatch
    with mpatch.object(sys, "argv", ["symphony", "triage", "--help"]):
        from symphony.main import main
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "triage" in captured.out or "triage" in captured.err
