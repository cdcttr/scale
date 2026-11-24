"""ColimaWorker: agent runs interactively in tmux inside a sandbox box.

A FakeDocker plays the box: it records every docker call and, like the real
in-box hooks, writes SessionStart / UserPromptSubmit / Stop events and a
transcript into the receipts folder mounted at /receipts.
"""
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from scale.config.schema import (AgentConfig, CodexConfig, SandboxConfig, TrackerConfig,
                                 WorkerConfig, WorkflowConfig, WorkspaceConfig)
from scale.publish import PUBLISH_OVERRIDE
from scale.tracker.models import Issue
from scale.worker.colima import ColimaWorker

SECRET = "ghp_REAL_TRACKER_TOKEN_should_never_reach_box"


def _config(root: Path, share: Path, publish="orchestrator", turn_timeout_ms=5000, **sb) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token=SECRET),
        workspace=WorkspaceConfig(root=str(root)),
        agent=AgentConfig(publish=publish),
        codex=CodexConfig(turn_timeout_ms=turn_timeout_ms),
        worker=WorkerConfig(sandbox=SandboxConfig(share_root=str(share), start_timeout_s=2, **sb)),
        prompt_template="Implement {{ issue.title }}.\nOpen a pull request targeting main.",
    )


def _issue() -> Issue:
    return Issue(
        id="i1", identifier="sudoku-s6y", number=1, title="hunt tests", description="d",
        state="active", labels=[], branch_name="scale/sudoku-s6y", url="bd://sudoku-s6y",
        priority=None, created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


class FakeDocker:
    def __init__(self, stop=True, start=True, usage=({"input_tokens": 10, "output_tokens": 5},)):
        self.calls: list[list[str]] = []
        self.stdin: list[str] = []
        self.receipts: Path | None = None
        self.stop, self.start, self.usage = stop, start, usage
        self.alive = False

    def _emit(self, **ev):
        with open(self.receipts / "events.jsonl", "a") as f:
            f.write(json.dumps({"ts": "t", "session_id": "s1", **ev}) + "\n")

    async def __call__(self, *args: str, input: str | None = None) -> tuple[int, str, str]:
        a = list(args)
        self.calls.append(a)
        if input is not None:
            self.stdin.append(input)
        if a[0] == "run":
            for i, x in enumerate(a):
                if x == "-v" and a[i + 1].endswith(":/receipts"):
                    self.receipts = Path(a[i + 1].rsplit(":", 1)[0])
            self.alive = True
            return 0, "cid\n", ""
        if a[0] == "inspect":
            return 0, "true\n" if self.alive else "false\n", ""
        if a[0] == "rm":
            self.alive = False
            return 0, "", ""
        if a[0] == "exec" and "new-session" in a and self.start:
            self._emit(hook_event_name="SessionStart", source="startup")
        if a[0] == "exec" and a[-1] == "Enter" and self.stop:
            prompt = self.stdin[-1]
            self._emit(hook_event_name="UserPromptSubmit", prompt=prompt)
            t = self.receipts / "transcript-s1.jsonl"
            t.write_text("".join(json.dumps({"type": "assistant", "message": {"usage": u}}) + "\n" for u in self.usage))
            self._emit(hook_event_name="Stop", last_assistant_message="DONE", transcript_path="/home/agent/x")
        return 0, "", ""


def _workspace(path: Path) -> MagicMock:
    ws = AsyncMock()
    ws.prepare = AsyncMock(return_value=path)
    ws.run_before_hook = AsyncMock()
    ws.run_after_hook = AsyncMock()
    return ws


@pytest.fixture
def dirs(tmp_path: Path):
    share = tmp_path / "agent-work"
    root = share / "workspaces"
    wsdir = root / "sudoku-s6y"
    wsdir.mkdir(parents=True)
    return share, root, wsdir


def _run_args(fake: FakeDocker) -> list[str]:
    return next(c for c in fake.calls if c[0] == "run")


@pytest.mark.asyncio
async def test_box_is_locked_down_and_gets_no_credentials(dirs):
    share, root, wsdir = dirs
    fake = FakeDocker()
    await ColimaWorker(_workspace(wsdir), _config(root, share), docker=fake).run(_issue(), _config(root, share), None)
    run = _run_args(fake)
    joined = " ".join(run)
    assert "--network agents-internal" in joined
    assert "--cap-drop ALL" in joined and "no-new-privileges" in joined
    mounts = [run[i + 1] for i, x in enumerate(run) if x == "-v"]
    assert mounts == [f"{wsdir}:/work", f"{fake.receipts}:/receipts"]  # nothing else from the host
    assert fake.receipts.is_relative_to(root) and not fake.receipts.is_relative_to(wsdir)  # receipts outside the repo
    assert SECRET not in joined and all(SECRET not in s for s in fake.stdin)
    assert "ANTHROPIC_AUTH_TOKEN=dummy-not-a-secret" in run


@pytest.mark.asyncio
async def test_prompt_is_pasted_commit_only_and_logged(dirs):
    share, root, wsdir = dirs
    fake = FakeDocker()
    cfg = _config(root, share)
    await ColimaWorker(_workspace(wsdir), cfg, docker=fake).run(_issue(), cfg, None)
    prompt = fake.stdin[-1]
    assert "Implement hunt tests." in prompt and prompt.endswith(PUBLISH_OVERRIDE)
    assert any("paste-buffer" in c and "-p" in c for c in fake.calls)   # bracketed paste, not typed
    sent = [json.loads(l) for l in (fake.receipts / "sent.jsonl").read_text().splitlines()]
    assert sent[-1]["sender"] == "orchestrator" and sent[-1]["prompt"] == prompt


@pytest.mark.asyncio
async def test_usage_events_and_box_removed(dirs):
    share, root, wsdir = dirs
    fake = FakeDocker(usage=({"input_tokens": 10, "output_tokens": 5}, {"input_tokens": 3, "output_tokens": 2}))
    events: list[dict] = []
    cfg = _config(root, share)
    await ColimaWorker(_workspace(wsdir), cfg, docker=fake).run(_issue(), cfg, None, on_event=events.append)
    usage = [e["message"]["usage"] for e in events if e.get("type") == "assistant"]
    assert usage == [{"input_tokens": 10, "output_tokens": 5}, {"input_tokens": 3, "output_tokens": 2}]
    assert any(e.get("type") == "scale:hook" and e.get("event") == "Stop" for e in events)
    assert fake.calls[-1][0] == "rm" and not fake.alive


@pytest.mark.asyncio
async def test_turn_timeout_raises_and_cleans_up(dirs):
    share, root, wsdir = dirs
    fake = FakeDocker(stop=False)
    cfg = _config(root, share, turn_timeout_ms=1500)
    with pytest.raises(RuntimeError, match="timed out"):
        await ColimaWorker(_workspace(wsdir), cfg, docker=fake).run(_issue(), cfg, None)
    assert not fake.alive


@pytest.mark.asyncio
async def test_session_must_start(dirs):
    share, root, wsdir = dirs
    fake = FakeDocker(start=False)
    cfg = _config(root, share)
    with pytest.raises(RuntimeError, match="did not start"):
        await ColimaWorker(_workspace(wsdir), cfg, docker=fake).run(_issue(), cfg, None)
    assert not fake.alive


def test_requires_orchestrator_publish(dirs):
    share, root, wsdir = dirs
    with pytest.raises(ValueError, match="publish"):
        ColimaWorker(_workspace(wsdir), _config(root, share, publish="agent"), docker=FakeDocker())


def test_workspace_root_must_be_inside_share(dirs, tmp_path):
    share, _, wsdir = dirs
    outside = tmp_path / "elsewhere"
    with pytest.raises(ValueError, match="share_root"):
        ColimaWorker(_workspace(wsdir), _config(outside, share), docker=FakeDocker())


@pytest.mark.asyncio
async def test_keep_box(dirs):
    share, root, wsdir = dirs
    fake = FakeDocker()
    cfg = _config(root, share, keep_box=True)
    await ColimaWorker(_workspace(wsdir), cfg, docker=fake).run(_issue(), cfg, None)
    assert fake.alive


def test_orchestrator_picks_sandbox_worker(dirs):
    from scale.orchestrator.core import Orchestrator
    share, root, _ = dirs
    orch = Orchestrator(_config(root, share), AsyncMock(), AsyncMock())
    assert isinstance(orch._make_worker(), ColimaWorker)


def test_orchestrator_rejects_sandbox_without_orchestrator_publish(dirs):
    from scale.orchestrator.core import Orchestrator
    share, root, _ = dirs
    with pytest.raises(ValueError, match="publish"):
        Orchestrator(_config(root, share, publish="agent"), AsyncMock(), AsyncMock())
