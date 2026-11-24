"""Live sandbox test: real Colima box, real Claude via agent-gate, real git.

Opt-in (uses a little model quota):  SCALE_SANDBOX_IT=1 uv run pytest -q tests/test_sandbox_live.py -s
Needs: Colima profile "agents", image agent-box, agent-gate running (gate/up.sh).
The "origin" is a local bare repo, so nothing is pushed to GitHub.
"""
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scale.config.schema import (AgentConfig, CodexConfig, SandboxConfig, TrackerConfig,
                                 WorkerConfig, WorkflowConfig, WorkspaceConfig)
from scale.publish import Publisher
from scale.tracker.models import Issue
from scale.worker.colima import ColimaWorker
from scale.workspace.manager import WorkspaceManager

pytestmark = pytest.mark.skipif(os.environ.get("SCALE_SANDBOX_IT") != "1", reason="live sandbox test (SCALE_SANDBOX_IT=1)")


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.asyncio
async def test_live_run_commits_in_box_and_orchestrator_publishes():
    base = Path.home() / "agent-work" / f"scale-it-{int(time.time())}"
    origin, root = base / "origin.git", base / "workspaces"
    root.mkdir(parents=True)
    _git(base, "init", "--bare", "-q", "-b", "main", str(origin))
    seed = base / "seed"; seed.mkdir()
    _git(seed, "init", "-q", "-b", "main"); (seed / "README.md").write_text("# demo\n")
    _git(seed, "add", "."); _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    _git(seed, "push", "-q", str(origin), "main")

    cfg = WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="unused-for-local-origin"),
        workspace=WorkspaceConfig(root=str(root)),
        agent=AgentConfig(publish="orchestrator"),
        codex=CodexConfig(turn_timeout_ms=240_000),
        worker=WorkerConfig(sandbox=SandboxConfig()),
        hooks={"after_create": f"git clone -q {origin} .", "before_run": "git checkout -q main"},
        prompt_template=(
            "Create a file named GREETING.txt containing exactly the line: hello from the sandbox\n"
            "Then commit it with the message: add greeting\n"
            "Reply with exactly: DONE"
        ),
    )
    issue = Issue(id="it", identifier="scale-it-1", number=1, title="greeting", description="",
                  state="active", labels=[], branch_name="scale/it-1", url="local",
                  priority=None, created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
    ws = WorkspaceManager(cfg)
    events: list[dict] = []
    await ColimaWorker(ws, cfg).run(issue, cfg, None, on_event=events.append)

    path = ws.path(issue)
    assert (path / "GREETING.txt").read_text().strip() == "hello from the sandbox"
    assert "add greeting" in _git(path, "log", "-1", "--format=%s")
    assert any(e.get("type") == "assistant" for e in events), "token usage should be reported"
    assert any(e.get("event") == "Stop" for e in events)

    scm = AsyncMock(); scm.fetch_pr_for_branch = AsyncMock(return_value=None)
    scm.create_pr = AsyncMock(return_value={"number": 1, "html_url": "local"})
    await Publisher(cfg, scm).publish(issue, path)
    assert _git(origin, "rev-parse", "scale/it-1") == _git(path, "rev-parse", "HEAD")
    print(f"\nreceipts: {sorted((root / '_receipts').rglob('*'))}")
