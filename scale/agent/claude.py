from __future__ import annotations
import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from scale.agent.stall import WorkspaceState, gather_workspace_state, get_head_sha
from scale.config.schema import CodexConfig

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class TurnResult:
    success: bool
    usage: Optional[TokenUsage]
    message: str = ""
    stderr: str = ""
    stall_info: Optional[WorkspaceState] = field(default=None)


def parse_stream_event(line: str) -> Optional[TurnResult]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None

    if event.get("type") == "result":
        subtype = event.get("subtype", "")
        usage_raw = event.get("usage")
        usage = None
        if usage_raw:
            usage = TokenUsage(
                input_tokens=usage_raw.get("input_tokens", 0),
                output_tokens=usage_raw.get("output_tokens", 0),
            )
        return TurnResult(
            success=(subtype == "success"),
            usage=usage,
            message=event.get("result", ""),
        )
    return None


def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


class ClaudeRunner:
    def __init__(self, config: CodexConfig) -> None:
        self._config = config

    def _build_cmd(self, prompt: str, is_continuation: bool, model: Optional[str] = None) -> list[str]:
        cmd = [
            self._config.command,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if is_continuation:
            cmd.append("--continue")
        if model is not None:
            cmd += ["--model", model]
        cmd += ["-p", prompt]
        return cmd

    async def run_turn(
        self,
        workspace: Path,
        prompt: str,
        is_continuation: bool,
        on_event: Optional[Callable[[dict], None]] = None,
        model: Optional[str] = None,
    ) -> TurnResult:
        cmd = self._build_cmd(prompt, is_continuation, model)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
            start_new_session=True,
        )

        start_sha = await get_head_sha(workspace)
        stall_timeout_s = self._config.stall_timeout_ms / 1000
        grace_period_s = self._config.stall_grace_period_ms / 1000
        heartbeat_s = self._config.stall_heartbeat_s

        result: Optional[TurnResult] = None
        last_activity = time.monotonic()
        stall_detected_at: Optional[float] = None
        stall_workspace_state: Optional[WorkspaceState] = None

        assert proc.stdout is not None
        try:
            while True:
                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=heartbeat_s,
                    )
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    elapsed_s = now - last_activity

                    if on_event:
                        on_event({"type": "scale:heartbeat", "elapsed_s": round(elapsed_s)})

                    if stall_detected_at is not None:
                        if (now - stall_detected_at) >= grace_period_s:
                            _kill_proc(proc)
                            await proc.wait()
                            logger.warning(
                                "stall grace period exceeded in %s after %.0fs",
                                workspace, now - stall_detected_at,
                            )
                            return TurnResult(
                                success=False,
                                usage=None,
                                message=f"Stall grace period exceeded after {now - stall_detected_at:.0f}s",
                                stall_info=stall_workspace_state,
                            )
                    elif elapsed_s >= stall_timeout_s:
                        ws_state = await gather_workspace_state(workspace, since_sha=start_sha)
                        stall_workspace_state = ws_state
                        stall_event: dict = {
                            "type": "scale:stall",
                            "elapsed_s": round(elapsed_s),
                            "uncommitted_files": ws_state.uncommitted_files,
                            "commits_since_start": ws_state.commits_since_start,
                            "status_summary": ws_state.status_summary,
                            "grace_period": ws_state.has_progress,
                        }
                        if on_event:
                            on_event(stall_event)
                        logger.warning(
                            "stall detected in %s — elapsed=%.0fs uncommitted=%d commits_since=%d grace=%s",
                            workspace, elapsed_s, ws_state.uncommitted_files,
                            ws_state.commits_since_start, ws_state.has_progress,
                        )
                        if ws_state.has_progress:
                            stall_detected_at = now
                        else:
                            _kill_proc(proc)
                            await proc.wait()
                            return TurnResult(
                                success=False,
                                usage=None,
                                message=f"Stall timeout after {elapsed_s:.0f}s with no workspace progress",
                                stall_info=ws_state,
                            )
                    continue

                if not raw_line:
                    break

                last_activity = time.monotonic()
                stall_detected_at = None

                line = raw_line.decode().strip()
                if not line:
                    continue
                parsed = parse_stream_event(line)
                if parsed is not None:
                    result = parsed
                if on_event:
                    try:
                        event = json.loads(line)
                        on_event(event)
                    except json.JSONDecodeError:
                        pass
        finally:
            _kill_proc(proc)
            await proc.wait()

        stderr_bytes = await proc.stderr.read() if proc.stderr else b""

        stderr_text = stderr_bytes.decode(errors="replace").strip()
        if proc.returncode != 0 and result is None:
            if stderr_text:
                logger.debug("claude stderr: %s", stderr_text)
            return TurnResult(success=False, usage=None, message=f"Exit code {proc.returncode}", stderr=stderr_text)
        if result is None:
            return TurnResult(success=False, usage=None, message="No result event received", stderr=stderr_text)
        result.stderr = stderr_text
        return result
