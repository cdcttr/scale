import asyncio
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from symphony.orchestrator.core import Orchestrator
from symphony.orchestrator.state import LiveSession
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
