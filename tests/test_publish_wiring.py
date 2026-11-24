"""agent.publish=orchestrator wiring: orchestrator publishes after runs; prompts say commit-only."""
import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scale.config.schema import AgentConfig, PollingConfig, TrackerConfig, WorkerConfig, WorkflowConfig
from scale.orchestrator.core import Orchestrator
from scale.orchestrator.state import LiveSession
from scale.publish import NoChangesError, PUBLISH_OVERRIDE, apply_publish_mode
from scale.tracker.models import Issue


def _config(publish="orchestrator", **kw) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok", terminal_labels=["scale:done"]),
        polling=PollingConfig(interval_ms=0),
        agent=AgentConfig(publish=publish),
        prompt_template="Work on {{ issue.title }}. Open a pull request targeting main.",
        **kw,
    )


def _issue() -> Issue:
    return Issue(
        id="i1", identifier="o/r#1", number=1, title="T", description="", state="active",
        labels=[], branch_name="scale/1", url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


def test_orchestrator_mode_rejects_ssh_workers():
    cfg = _config(worker=WorkerConfig(ssh_hosts=["box"]))
    with pytest.raises(ValueError, match="ssh"):
        Orchestrator(cfg, AsyncMock(), AsyncMock())


def test_orchestrator_mode_requires_scm():
    with pytest.raises(ValueError, match="SCM"):
        Orchestrator(_config(), AsyncMock(), None)


def test_agent_mode_unchanged():
    Orchestrator(_config(publish="agent"), AsyncMock(), None)  # no error
    assert apply_publish_mode("p", _config(publish="agent")) == "p"


def test_prompt_override_in_orchestrator_mode():
    out = apply_publish_mode("Continue. Open a pull request when done.", _config())
    assert "Open a pull request when done." not in out
    assert out.endswith(PUBLISH_OVERRIDE)


async def _run(orch: Orchestrator, publish_effect=None):
    issue = _issue()
    orch._state.running[issue.id] = LiveSession(issue=issue, task=asyncio.create_task(asyncio.sleep(0)))
    orch._state.claimed.add(issue.id)

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        return None

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch("scale.orchestrator.core.Publisher") as MockPub, \
         patch.object(orch._workspace, "remove", AsyncMock()), \
         patch.object(orch, "_record_stats", AsyncMock()), \
         patch.object(orch, "_post_attempt_summary", AsyncMock()):
        w = MagicMock(); w.run = _mock_run; MockWorker.return_value = w
        pub = MagicMock(); pub.publish = AsyncMock(return_value={"number": 3, "html_url": "u"}, side_effect=publish_effect)
        MockPub.return_value = pub
        await orch._run_worker(issue, attempt=None)
    return issue, pub


@pytest.mark.asyncio
async def test_successful_run_is_published_before_done():
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker, AsyncMock())
    issue, pub = await _run(orch)
    pub.publish.assert_awaited_once()
    assert pub.publish.await_args.args[0] is issue
    tracker.add_labels.assert_awaited_with(1, ["scale:done"])


@pytest.mark.asyncio
async def test_no_commits_means_retry_not_done():
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker, AsyncMock())
    issue, _ = await _run(orch, publish_effect=NoChangesError("nothing committed"))
    tracker.add_labels.assert_not_called()          # not marked done
    retries = [e for e in orch._state.retry_queue if e.issue.id == issue.id]
    assert retries and "nothing committed" in retries[0].error   # retried, with the reason recorded


@pytest.mark.asyncio
async def test_agent_mode_does_not_publish():
    tracker = AsyncMock()
    orch = Orchestrator(_config(publish="agent"), tracker, AsyncMock())
    _, pub = await _run(orch)
    pub.publish.assert_not_called()
