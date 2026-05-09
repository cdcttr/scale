from __future__ import annotations
import asyncio
import json
import logging
import shlex
from typing import Callable, Optional

from scale.agent.claude import ClaudeRunner, parse_stream_event
from scale.config.schema import WorkflowConfig
from scale.prompt.renderer import render_prompt
from scale.tracker.models import Issue
from scale.worker.base import Worker
from scale.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

_CONTINUATION_PROMPT = (
    "Continue working on the task. Review any progress already made in this "
    "workspace and pick up where you left off. Open a pull request when done."
)


class SSHWorker(Worker):
    def __init__(
        self,
        ssh_host: str,
        workspace: WorkspaceManager,
        config: WorkflowConfig,
    ) -> None:
        self._host = ssh_host
        self._workspace = workspace
        self._config = config

    def _build_remote_cmd(self, local_cmd: list[str]) -> list[str]:
        inner = " ".join(shlex.quote(a) for a in local_cmd)
        return ["ssh", "-T", self._host, "bash -lc " + shlex.quote(inner)]

    async def run(
        self,
        issue: Issue,
        config: WorkflowConfig,
        attempt: Optional[int],
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        workspace_path = await self._workspace.prepare(issue)
        await self._workspace.run_before_hook(issue)

        runner = ClaudeRunner(config.codex)

        try:
            for turn_idx in range(config.agent.max_turns):
                is_continuation = turn_idx > 0
                prompt = (
                    _CONTINUATION_PROMPT
                    if is_continuation
                    else render_prompt(config.prompt_template, issue, attempt)
                )

                local_cmd = runner._build_cmd(prompt, is_continuation)
                remote_cmd = self._build_remote_cmd(local_cmd)

                logger.info(
                    "issue_id=%s host=%s turn=%d starting",
                    issue.id, self._host, turn_idx + 1,
                )

                proc = await asyncio.create_subprocess_exec(
                    *remote_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                result = None
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
                            on_event(json.loads(line))
                        except json.JSONDecodeError:
                            pass

                await proc.wait()

                if result is None or not result.success:
                    msg = result.message if result else f"exit {proc.returncode}"
                    raise RuntimeError(f"Remote turn {turn_idx + 1} failed: {msg}")

                logger.info(
                    "issue_id=%s host=%s turn=%d succeeded",
                    issue.id, self._host, turn_idx + 1,
                )
                break
        finally:
            await self._workspace.run_after_hook(issue)
