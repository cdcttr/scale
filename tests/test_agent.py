import asyncio
import json
import os
import signal
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
from scale.agent.claude import ClaudeRunner, parse_stream_event, TurnResult, TokenUsage
from scale.agent.stall import WorkspaceState
from scale.config.schema import CodexConfig


@pytest.fixture(autouse=True)
def _patch_killpg(monkeypatch):
    monkeypatch.setattr("scale.agent.claude.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("scale.agent.claude.os.killpg", lambda pgid, sig: None)


def _runner() -> ClaudeRunner:
    return ClaudeRunner(CodexConfig())


def _make_proc(lines: list[str], returncode: int = 0):
    class _Stdout:
        def __init__(self, ls: list[str]) -> None:
            self._lines = [(ln + "\n").encode() for ln in ls]
            self._index = 0

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for line in self._lines:
                yield line

        async def readline(self) -> bytes:
            if self._index < len(self._lines):
                line = self._lines[self._index]
                self._index += 1
                return line
            return b""

    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = _Stdout(lines)
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc

def _event(type_: str, **kwargs) -> str:
    return json.dumps({"type": type_, **kwargs})

def test_parse_assistant_event_returns_none():
    line = _event("assistant", message={"content": []})
    result = parse_stream_event(line)
    assert result is None

def test_parse_success_result():
    line = _event(
        "result",
        subtype="success",
        result="Done.",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    result = parse_stream_event(line)
    assert isinstance(result, TurnResult)
    assert result.success is True
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50

def test_parse_error_result():
    line = _event("result", subtype="error", result="Something failed.")
    result = parse_stream_event(line)
    assert isinstance(result, TurnResult)
    assert result.success is False
    assert result.usage is None

def test_parse_unknown_event_returns_none():
    line = _event("system", subtype="init")
    result = parse_stream_event(line)
    assert result is None

def test_token_usage_total():
    usage = TokenUsage(input_tokens=200, output_tokens=80)
    assert usage.total == 280


def test_build_cmd_no_continuation():
    cmd = _runner()._build_cmd("do the thing", False)
    assert "--continue" not in cmd
    assert "-p" in cmd
    assert "do the thing" in cmd


def test_build_cmd_with_continuation():
    cmd = _runner()._build_cmd("continue", True)
    assert "--continue" in cmd


@pytest.mark.asyncio
async def test_run_turn_success(tmp_path: Path):
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "Done",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))):
        result = await _runner().run_turn(tmp_path, "prompt", False)
    assert result.success
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5


@pytest.mark.asyncio
async def test_run_turn_error_subtype(tmp_path: Path):
    event = json.dumps({"type": "result", "subtype": "error", "result": "crash"})
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))):
        result = await _runner().run_turn(tmp_path, "prompt", False)
    assert not result.success


@pytest.mark.asyncio
async def test_run_turn_nonzero_exit_no_result(tmp_path: Path):
    proc = _make_proc([], returncode=1)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await _runner().run_turn(tmp_path, "prompt", False)
    assert not result.success
    assert "Exit code 1" in result.message


@pytest.mark.asyncio
async def test_run_turn_zero_exit_no_result(tmp_path: Path):
    proc = _make_proc([], returncode=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await _runner().run_turn(tmp_path, "prompt", False)
    assert not result.success
    assert "No result event" in result.message


@pytest.mark.asyncio
async def test_run_turn_fires_on_event(tmp_path: Path):
    events = [
        json.dumps({"type": "assistant", "text": "thinking"}),
        json.dumps({
            "type": "result", "subtype": "success", "result": "Done",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
    ]
    seen: list[dict] = []
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc(events))):
        await _runner().run_turn(tmp_path, "prompt", False, on_event=seen.append)
    assert len(seen) == 2
    assert seen[0]["type"] == "assistant"


def test_build_cmd_with_model():
    runner = ClaudeRunner(CodexConfig())
    cmd = runner._build_cmd("prompt", False, model="claude-haiku-4-5-20251001")
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "claude-haiku-4-5-20251001"


def test_build_cmd_without_model_omits_flag():
    runner = ClaudeRunner(CodexConfig())
    cmd = runner._build_cmd("prompt", False, model=None)
    assert "--model" not in cmd


@pytest.mark.asyncio
async def test_run_turn_passes_model_to_cmd(tmp_path: Path):
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "Done",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))) as mock_exec, \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)):
        await _runner().run_turn(tmp_path, "prompt", False, model="claude-haiku-4-5-20251001")
    cmd_args = mock_exec.call_args[0]
    assert "--model" in cmd_args
    idx = list(cmd_args).index("--model")
    assert cmd_args[idx + 1] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_run_turn_uses_large_stream_limit(tmp_path: Path):
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "done",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))) as mock_exec, \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)):
        await _runner().run_turn(tmp_path, "prompt", False)
    assert mock_exec.call_args.kwargs.get("limit") == 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_run_turn_large_line_succeeds(tmp_path: Path):
    large_payload = "x" * (65 * 1024)
    event = json.dumps({
        "type": "result", "subtype": "success", "result": large_payload,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    line_bytes = (event + "\n").encode()
    reader = asyncio.StreamReader(limit=8 * 1024 * 1024)
    reader.feed_data(line_bytes)
    reader.feed_eof()

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = reader
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await _runner().run_turn(tmp_path, "prompt", False)
    assert result.success


# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------

def _stall_config(stall_ms: int = 50, grace_ms: int = 50, heartbeat_s: float = 0.02) -> CodexConfig:
    return CodexConfig(
        stall_timeout_ms=stall_ms,
        stall_grace_period_ms=grace_ms,
        stall_heartbeat_s=heartbeat_s,
    )


def _make_blocking_proc():
    class _BlockingStdout:
        async def readline(self) -> bytes:
            await asyncio.sleep(1000)
            return b""

    proc = MagicMock()
    proc.stdout = _BlockingStdout()
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    proc.returncode = -9
    return proc


@pytest.mark.asyncio
async def test_stall_terminates_with_no_progress(tmp_path: Path):
    proc = _make_blocking_proc()
    no_progress = WorkspaceState(uncommitted_files=0, commits_since_start=0, status_summary="")

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)), \
         patch("scale.agent.claude.gather_workspace_state", AsyncMock(return_value=no_progress)):
        result = await ClaudeRunner(_stall_config()).run_turn(tmp_path, "prompt", False)

    assert not result.success
    assert "Stall" in result.message
    assert result.stall_info is not None
    assert not result.stall_info.has_progress
    proc.wait.assert_called()


@pytest.mark.asyncio
async def test_stall_emits_stall_event(tmp_path: Path):
    proc = _make_blocking_proc()
    no_progress = WorkspaceState(uncommitted_files=0, commits_since_start=0, status_summary="")
    events: list[dict] = []

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)), \
         patch("scale.agent.claude.gather_workspace_state", AsyncMock(return_value=no_progress)):
        await ClaudeRunner(_stall_config()).run_turn(tmp_path, "prompt", False, on_event=events.append)

    stall_events = [e for e in events if e.get("type") == "scale:stall"]
    assert len(stall_events) == 1
    assert "elapsed_s" in stall_events[0]
    assert "uncommitted_files" in stall_events[0]


@pytest.mark.asyncio
async def test_stall_emits_heartbeat_events(tmp_path: Path):
    proc = _make_blocking_proc()
    no_progress = WorkspaceState(uncommitted_files=0, commits_since_start=0, status_summary="")
    events: list[dict] = []

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)), \
         patch("scale.agent.claude.gather_workspace_state", AsyncMock(return_value=no_progress)):
        await ClaudeRunner(_stall_config(stall_ms=200, heartbeat_s=0.02)).run_turn(
            tmp_path, "prompt", False, on_event=events.append
        )

    heartbeats = [e for e in events if e.get("type") == "scale:heartbeat"]
    assert len(heartbeats) >= 1


@pytest.mark.asyncio
async def test_stall_grants_grace_period_when_progress(tmp_path: Path):
    proc = _make_blocking_proc()
    with_progress = WorkspaceState(uncommitted_files=3, commits_since_start=1, status_summary="M foo.py")

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)), \
         patch("scale.agent.claude.gather_workspace_state", AsyncMock(return_value=with_progress)):
        result = await ClaudeRunner(_stall_config(stall_ms=50, grace_ms=50)).run_turn(
            tmp_path, "prompt", False
        )

    assert not result.success
    assert "grace period" in result.message.lower()
    assert result.stall_info is not None
    assert result.stall_info.has_progress
    proc.wait.assert_called()


@pytest.mark.asyncio
async def test_stall_stall_event_includes_grace_period_flag(tmp_path: Path):
    proc = _make_blocking_proc()
    with_progress = WorkspaceState(uncommitted_files=2, commits_since_start=0, status_summary="M bar.py")
    events: list[dict] = []

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)), \
         patch("scale.agent.claude.gather_workspace_state", AsyncMock(return_value=with_progress)):
        await ClaudeRunner(_stall_config()).run_turn(tmp_path, "prompt", False, on_event=events.append)

    stall_events = [e for e in events if e.get("type") == "scale:stall"]
    assert stall_events[0]["grace_period"] is True
    assert stall_events[0]["uncommitted_files"] == 2


@pytest.mark.asyncio
async def test_normal_turn_not_affected_by_stall_config(tmp_path: Path):
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "Done",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    proc = _make_proc([event])

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)):
        result = await ClaudeRunner(_stall_config(stall_ms=5000)).run_turn(tmp_path, "prompt", False)

    assert result.success
    assert result.stall_info is None


@pytest.mark.asyncio
async def test_run_turn_uses_start_new_session(tmp_path: Path):
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "done",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))) as mock_exec:
        await _runner().run_turn(tmp_path, "prompt", False)
    claude_calls = [c for c in mock_exec.call_args_list if c.args and "claude" in str(c.args[0])]
    assert claude_calls, "claude subprocess not found in calls"
    assert claude_calls[0].kwargs.get("start_new_session") is True


@pytest.mark.asyncio
async def test_run_turn_kills_process_group_on_cancellation(tmp_path: Path):
    async def _hanging_readline():
        await asyncio.sleep(9999)
        return b""

    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = -9
    proc.stdout = MagicMock()
    proc.stdout.readline = _hanging_readline
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        with patch("scale.agent.claude.os.getpgid", return_value=12345):
            with patch("scale.agent.claude.os.killpg") as mock_killpg:
                task = asyncio.create_task(_runner().run_turn(tmp_path, "prompt", False))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    mock_killpg.assert_called_with(12345, signal.SIGKILL)
    proc.wait.assert_called()


@pytest.mark.asyncio
async def test_run_turn_handles_process_already_gone(tmp_path: Path):
    async def _hanging_readline():
        await asyncio.sleep(9999)
        return b""

    proc = MagicMock()
    proc.pid = 99999
    proc.returncode = -9
    proc.stdout = MagicMock()
    proc.stdout.readline = _hanging_readline
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        with patch("scale.agent.claude.os.getpgid", return_value=99999):
            with patch("scale.agent.claude.os.killpg", side_effect=ProcessLookupError):
                task = asyncio.create_task(_runner().run_turn(tmp_path, "prompt", False))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    proc.wait.assert_called()


# ---------------------------------------------------------------------------
# log_path / log_label
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_turn_writes_log_header_and_prompt(tmp_path: Path):
    log_file = tmp_path / "test.log"
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "Done",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)):
        await _runner().run_turn(tmp_path, "my test prompt", False, log_path=log_file, log_label="TestRun")

    content = log_file.read_text()
    assert "TestRun" in content
    assert "my test prompt" in content
    assert "PROMPT:" in content


@pytest.mark.asyncio
async def test_run_turn_writes_events_to_log(tmp_path: Path):
    log_file = tmp_path / "events.log"
    lines = [
        json.dumps({"type": "assistant", "text": "thinking"}),
        json.dumps({
            "type": "result", "subtype": "success", "result": "Done",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
    ]
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc(lines))), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)):
        await _runner().run_turn(tmp_path, "prompt", False, log_path=log_file)

    content = log_file.read_text()
    assert '"type": "assistant"' in content
    assert "RESULT: success=True" in content


@pytest.mark.asyncio
async def test_run_turn_writes_tokens_to_log(tmp_path: Path):
    log_file = tmp_path / "tokens.log"
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "Done",
        "usage": {"input_tokens": 42, "output_tokens": 7},
    })
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)):
        await _runner().run_turn(tmp_path, "prompt", False, log_path=log_file)

    content = log_file.read_text()
    assert "TOKENS: in=42 out=7" in content


@pytest.mark.asyncio
async def test_run_turn_no_log_file_when_path_is_none(tmp_path: Path):
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "Done",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)):
        await _runner().run_turn(tmp_path, "prompt", False)

    assert not list(tmp_path.glob("*.log"))


@pytest.mark.asyncio
async def test_run_turn_still_calls_on_event_when_log_path_set(tmp_path: Path):
    log_file = tmp_path / "combined.log"
    lines = [
        json.dumps({"type": "assistant", "text": "thinking"}),
        json.dumps({
            "type": "result", "subtype": "success", "result": "Done",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
    ]
    seen: list[dict] = []
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc(lines))), \
         patch("scale.agent.claude.get_head_sha", AsyncMock(return_value=None)):
        await _runner().run_turn(tmp_path, "prompt", False, on_event=seen.append, log_path=log_file)

    assert any(e.get("type") == "assistant" for e in seen)
    assert log_file.exists()
