import asyncio
import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from symphony.agent.claude import TurnResult, TokenUsage
from symphony.config.schema import WorkflowConfig, TrackerConfig, AgentConfig
from symphony.tracker.models import Issue
from symphony.worker.local import LocalWorker
from symphony.worker.ssh import SSHWorker


def _config(max_turns: int = 3) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        agent=AgentConfig(max_turns=max_turns),
        prompt_template="Work on {{ issue.title }}.",
    )


def _issue() -> Issue:
    return Issue(
        id="i1", identifier="o/r#1", number=1,
        title="Fix it", description="desc", state="active",
        labels=[], branch_name="symphony/1-fix-it",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )


def _mock_workspace(tmp_path: Path) -> MagicMock:
    ws = AsyncMock()
    ws.prepare = AsyncMock(return_value=tmp_path)
    ws.run_before_hook = AsyncMock()
    ws.run_after_hook = AsyncMock()
    return ws


def _make_proc(lines: list[str], returncode: int = 0) -> MagicMock:
    class _Stdout:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for line in lines:
                yield (line + "\n").encode()

    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = _Stdout()
    proc.wait = AsyncMock()
    return proc


# ---------------------------------------------------------------------------
# LocalWorker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_worker_success_on_first_turn(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = LocalWorker(ws, config)
    worker._runner.run_turn = AsyncMock(
        return_value=TurnResult(success=True, usage=TokenUsage(10, 5))
    )

    await worker.run(_issue(), config, attempt=None)

    worker._runner.run_turn.assert_called_once()
    ws.run_after_hook.assert_called_once()


@pytest.mark.asyncio
async def test_local_worker_raises_on_failed_turn(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = LocalWorker(ws, config)
    worker._runner.run_turn = AsyncMock(
        return_value=TurnResult(success=False, usage=None, message="crashed")
    )

    with pytest.raises(RuntimeError, match="Turn 1 failed"):
        await worker.run(_issue(), config, attempt=None)

    ws.run_after_hook.assert_called_once()  # finally block always runs


@pytest.mark.asyncio
async def test_local_worker_after_hook_runs_on_failure(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = LocalWorker(ws, config)
    worker._runner.run_turn = AsyncMock(
        return_value=TurnResult(success=False, usage=None, message="boom")
    )

    with pytest.raises(RuntimeError):
        await worker.run(_issue(), config, attempt=None)

    ws.run_after_hook.assert_called_once()


@pytest.mark.asyncio
async def test_local_worker_first_turn_uses_rendered_prompt(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = LocalWorker(ws, config)

    prompts_used: list[tuple[str, bool]] = []

    async def _capture_turn(workspace, prompt, is_continuation, on_event=None):
        prompts_used.append((prompt, is_continuation))
        return TurnResult(success=True, usage=TokenUsage(1, 1))

    worker._runner.run_turn = _capture_turn

    await worker.run(_issue(), config, attempt=None)

    assert len(prompts_used) == 1
    prompt, is_cont = prompts_used[0]
    assert not is_cont
    assert "Fix it" in prompt  # rendered from template


# ---------------------------------------------------------------------------
# SSHWorker
# ---------------------------------------------------------------------------

def test_ssh_worker_build_remote_cmd():
    ws = MagicMock()
    worker = SSHWorker("user@host", ws, _config())
    local_cmd = ["claude", "--print", "-p", "hello world"]
    remote = worker._build_remote_cmd(local_cmd)
    assert remote[:3] == ["ssh", "-T", "user@host"]
    assert "bash -lc" in remote[3]
    assert "hello world" in remote[3]


@pytest.mark.asyncio
async def test_ssh_worker_success(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = SSHWorker("user@host", ws, config)

    result_event = json.dumps({
        "type": "result", "subtype": "success", "result": "Done",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    proc = _make_proc([result_event])

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        await worker.run(_issue(), config, attempt=None)

    ws.run_after_hook.assert_called_once()


@pytest.mark.asyncio
async def test_ssh_worker_raises_on_failure(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = SSHWorker("user@host", ws, config)

    proc = _make_proc([], returncode=1)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        with pytest.raises(RuntimeError, match="Remote turn 1 failed"):
            await worker.run(_issue(), config, attempt=None)

    ws.run_after_hook.assert_called_once()


@pytest.mark.asyncio
async def test_ssh_worker_fires_on_event(tmp_path: Path):
    config = _config()
    ws = _mock_workspace(tmp_path)
    worker = SSHWorker("user@host", ws, config)

    events = [
        json.dumps({"type": "assistant", "text": "thinking"}),
        json.dumps({
            "type": "result", "subtype": "success", "result": "Done",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
    ]
    proc = _make_proc(events)
    seen: list[dict] = []

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        await worker.run(_issue(), config, attempt=None, on_event=seen.append)

    assert any(e.get("type") == "assistant" for e in seen)
