"""Attempt summaries must report uncommitted work (gci-e9v)."""
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scale.config.schema import AgentConfig, PollingConfig, TrackerConfig, WorkflowConfig
from scale.orchestrator.core import Orchestrator
from scale.orchestrator.state import LiveSession
from scale.tracker.models import Issue


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    origin, seed, ws = tmp_path / "o.git", tmp_path / "seed", tmp_path / "ws"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
    seed.mkdir(); _git(seed, "init", "-q", "-b", "trunk")
    (seed / "a.py").write_text("x = 1\n"); (seed / "b.py").write_text("y = 2\n")
    _git(seed, "add", "."); _git(seed, "commit", "-qm", "init"); _git(seed, "push", "-q", str(origin), "trunk")
    _git(tmp_path, "clone", "-q", str(origin), str(ws))
    return ws


def _orch(default_branch="trunk") -> Orchestrator:
    cfg = WorkflowConfig(tracker=TrackerConfig(repo="o/r", api_token="t", default_branch=default_branch),
                         polling=PollingConfig(interval_ms=0), agent=AgentConfig(), prompt_template="x")
    return Orchestrator(cfg, AsyncMock())


@pytest.mark.asyncio
async def test_modified_tracked_file_is_reported(ws):
    (ws / "a.py").write_text("x = 1\n# agent added tests here\n")      # tracked, modified, uncommitted
    state = await _orch()._collect_workspace_state(ws)
    assert state["uncommitted_files"] == ["a.py"]
    assert state["commits"] == [] and state["new_files"] == []


@pytest.mark.asyncio
async def test_staged_deleted_and_new_files(ws):
    (ws / "b.py").unlink()
    (ws / "c.py").write_text("z = 3\n"); _git(ws, "add", "c.py")       # staged new file
    (ws / "notes.txt").write_text("untracked\n")
    state = await _orch()._collect_workspace_state(ws)
    assert set(state["uncommitted_files"]) == {"b.py", "c.py"}
    assert state["new_files"] == ["notes.txt"]


@pytest.mark.asyncio
async def test_uses_configured_default_branch(ws):
    (ws / "a.py").write_text("x = 2\n"); _git(ws, "commit", "-qam", "agent: change a")
    state = await _orch("trunk")._collect_workspace_state(ws)
    assert state["modified_files"] == ["a.py"]
    assert len(state["commits"]) == 1


@pytest.mark.asyncio
async def test_summary_comment_lists_uncommitted_work(ws):
    (ws / "a.py").write_text("x = 1\n# wip\n")
    orch = _orch()
    issue = Issue(id="i1", identifier="o/r#1", number=1, title="T", description="", state="active",
                  labels=[], branch_name="scale/1", url="u", priority=None,
                  created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
    orch._workspace.path = lambda _issue: ws
    session = LiveSession(issue=issue, task=None)
    await orch._post_attempt_summary(issue, session, attempt=None)
    body = orch._tracker.post_comment.await_args.args[1]
    assert "Uncommitted changes" in body and "`a.py`" in body
    assert "No file changes detected" not in body
