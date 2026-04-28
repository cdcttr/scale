from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from symphony.config.schema import CodexConfig

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


class ClaudeRunner:
    def __init__(self, config: CodexConfig) -> None:
        self._config = config

    def _build_cmd(self, prompt: str, is_continuation: bool) -> list[str]:
        cmd = [
            self._config.command,
            "--print",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--max-turns", "1",
        ]
        if is_continuation:
            cmd.append("--continue")
        cmd += ["-p", prompt]
        return cmd

    async def run_turn(
        self,
        workspace: Path,
        prompt: str,
        is_continuation: bool,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> TurnResult:
        cmd = self._build_cmd(prompt, is_continuation)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        result: Optional[TurnResult] = None

        assert proc.stdout is not None
        async for raw_line in proc.stdout:
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

        await proc.wait()

        if proc.returncode != 0 and result is None:
            return TurnResult(success=False, usage=None, message=f"Exit code {proc.returncode}")
        if result is None:
            return TurnResult(success=False, usage=None, message="No result event received")
        return result
