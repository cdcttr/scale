"""Blocked-with-reason vs no-op (gci-t1h).

Agents end their final message with a status line:
    SCALE-STATUS: done
    SCALE-STATUS: blocked: <reason>
Blocked runs are escalated (label + comment, workspace kept, no publish, no retry);
runs without commits and without a blocked status are retried as before.
"""
import asyncio
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scale.agent.status import parse_agent_status
from scale.config.schema import AgentConfig, PollingConfig, TrackerConfig, WorkflowConfig
from scale.orchestrator.core import Orchestrator
from scale.orchestrator.state import LiveSession
from scale.publish import NoChangesError, PUBLISH_OVERRIDE
from scale.tracker.models import Issue


# ---- parsing -------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("All done.\nSCALE-STATUS: done", ("done", None)),
    ("No python.\n\nSCALE-STATUS: blocked: no python3 in the sandbox", ("blocked", "no python3 in the sandbox")),
    ("scale-status: Blocked: needs network access to pypi.org", ("blocked", "needs network access to pypi.org")),
    ("SCALE-STATUS: blocked", ("blocked", "")),
    ("SCALE-STATUS: done\n...later...\nSCALE-STATUS: blocked: changed my mind", ("blocked", "changed my mind")),
    ("I couldn't finish this task.", (None, None)),
    ("", (None, None)),
    ("The doc says to write SCALE-STATUS: done at the end", (None, None)),  # must be its own line
])
def test_parse_agent_status(text, expected):
    assert parse_agent_status(text) == expected


def test_prompt_requires_status_line():
    assert "SCALE-STATUS: done" in PUBLISH_OVERRIDE
    assert "SCALE-STATUS: blocked:" in PUBLISH_OVERRIDE


# ---- orchestrator --------------------------------------------------------

def _config() -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok", terminal_labels=["scale:done"]),
        polling=PollingConfig(interval_ms=0),
        agent=AgentConfig(publish="orchestrator"),
        prompt_template="x",
    )


def _issue() -> Issue:
    return Issue(id="i1", identifier="o/r#1", number=1, title="T", description="", state="active",
                 labels=[], branch_name="scale/1", url="u", priority=None,
                 created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))


async def _run(final_message: str | None, publish_effect=None):
    tracker = AsyncMock()
    orch = Orchestrator(_config(), tracker, AsyncMock())
    issue = _issue()
    orch._state.running[issue.id] = LiveSession(issue=issue, task=asyncio.create_task(asyncio.sleep(0)))
    orch._state.claimed.add(issue.id)

    async def _mock_run(iss, cfg, attempt, on_event=None, previous_attempt_summary=None):
        if final_message is not None and on_event:
            on_event({"type": "scale:final", "message": final_message})

    remove = AsyncMock()
    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch("scale.orchestrator.core.Publisher") as MockPub, \
         patch.object(orch._workspace, "remove", remove), \
         patch.object(orch, "_record_stats", AsyncMock()), \
         patch.object(orch, "_post_attempt_summary", AsyncMock()) as summary:
        w = MagicMock(); w.run = _mock_run; MockWorker.return_value = w
        pub = MagicMock(); pub.publish = AsyncMock(return_value={"number": 3, "html_url": "u"}, side_effect=publish_effect)
        MockPub.return_value = pub
        await orch._run_worker(issue, attempt=None)
        await asyncio.sleep(0)
    return orch, tracker, pub, remove, summary


@pytest.mark.asyncio
async def test_blocked_is_escalated_not_retried_not_published():
    msg = "I couldn't run the tests: no python3.\n\nSCALE-STATUS: blocked: no python3 in the sandbox"
    orch, tracker, pub, remove, summary = await _run(msg)
    pub.publish.assert_not_called()
    tracker.add_labels.assert_awaited_once_with(1, ["scale:blocked"])
    body = tracker.post_comment.await_args.args[1]
    assert "<!-- scale-blocked -->" in body and "no python3 in the sandbox" in body
    assert "I couldn't run the tests" in body                  # the agent's own words, verbatim
    assert orch._state.retry_queue == []                       # no retry
    remove.assert_not_called()                                 # keep partial work for the human
    summary.assert_awaited()                                   # receipts list uncommitted work
    assert "i1" not in orch._state.claimed


@pytest.mark.asyncio
async def test_done_without_commits_is_retried():
    orch, tracker, pub, _, _ = await _run("Finished.\nSCALE-STATUS: done", publish_effect=NoChangesError("no commits"))
    assert any(e.issue.id == "i1" for e in orch._state.retry_queue)
    tracker.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_no_status_without_commits_is_retried():
    orch, _, _, _, _ = await _run(None, publish_effect=NoChangesError("no commits"))
    assert any(e.issue.id == "i1" for e in orch._state.retry_queue)


@pytest.mark.asyncio
async def test_done_with_commits_publishes():
    orch, tracker, pub, remove, _ = await _run("Committed.\nSCALE-STATUS: done")
    pub.publish.assert_awaited_once()
    tracker.add_labels.assert_awaited_with(1, ["scale:done"])


# ---- trackers stop dispatching blocked items --------------------------------

def test_github_blocked_label_is_ignored():
    from scale.tracker.github import GitHubClient
    gh = GitHubClient(TrackerConfig(repo="o/r", api_token="t"))
    assert gh._resolve_state(["scale:blocked"], "open") == "ignored"
    assert gh._resolve_state([], "open") == "active"


@pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed")
@pytest.mark.asyncio
async def test_beads_blocked_label_sets_status_and_stops_dispatch(tmp_path):
    from scale.tracker.beads import BeadsTracker, bead_number
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "BD_NON_INTERACTIVE": "1"}
    run = lambda *a: subprocess.run(["bd", *a], cwd=tmp_path, check=True, capture_output=True, text=True, env=env).stdout.strip()
    run("init", "--prefix", "w", "--non-interactive", "--skip-agents", "--skip-hooks", "--quiet")
    bid = run("q", "Agent task"); run("label", "add", bid, "agent-ok")
    t = BeadsTracker(TrackerConfig(kind="beads", beads_path=str(tmp_path), repo="o/r", api_token="t", active_labels=["agent-ok"]))
    assert [i.id for i in await t.fetch_candidate_issues()] == [bid]
    await t.add_labels(bead_number(bid), ["scale:blocked"])
    assert await t.fetch_candidate_issues() == []
    assert '"status":"blocked"' in run("show", bid, "--json").replace(" ", "")


# ---- workers report the final message ----------------------------------------

@pytest.mark.asyncio
async def test_colima_worker_reports_final_message(tmp_path):
    from tests.test_colima_worker import FakeDocker, _config as ccfg, _issue as cissue, _workspace
    share = tmp_path / "agent-work"; root = share / "w"; wsdir = root / "x"; wsdir.mkdir(parents=True)
    from scale.worker.colima import ColimaWorker
    events: list[dict] = []
    cfg = ccfg(root, share)
    await ColimaWorker(_workspace(wsdir), cfg, docker=FakeDocker()).run(cissue(), cfg, None, on_event=events.append)
    assert {"type": "scale:final", "message": "DONE"} in events


@pytest.mark.asyncio
async def test_local_worker_reports_final_message(tmp_path):
    from scale.agent.claude import TurnResult
    from scale.worker.local import LocalWorker
    ws = AsyncMock(); ws.prepare = AsyncMock(return_value=tmp_path)
    cfg = WorkflowConfig(tracker=TrackerConfig(repo="o/r", api_token="t"), prompt_template="x")
    worker = LocalWorker(ws, cfg)
    worker._runner.run_turn = AsyncMock(return_value=TurnResult(success=True, usage=None, message="ok\nSCALE-STATUS: done"))
    events: list[dict] = []
    await worker.run(_issue(), cfg, None, on_event=events.append)
    assert {"type": "scale:final", "message": "ok\nSCALE-STATUS: done"} in events
