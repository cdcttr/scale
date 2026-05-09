from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from scale.agent.claude import ClaudeRunner
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


class LocalWorker(Worker):
    def __init__(self, workspace: WorkspaceManager, config: WorkflowConfig) -> None:
        self._workspace = workspace
        self._config = config
        self._runner = ClaudeRunner(config.codex)

    async def run(
        self,
        issue: Issue,
        config: WorkflowConfig,
        attempt: Optional[int],
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        workspace_path = await self._workspace.prepare(issue)
        await self._workspace.run_before_hook(issue)

        log_path = workspace_path / "agent.log"

        def _log_event(event: dict) -> None:
            with open(log_path, "a") as f:
                f.write(json.dumps(event) + "\n")
            if on_event:
                on_event(event)

        try:
            for turn_idx in range(config.agent.max_turns):
                is_continuation = turn_idx > 0
                prompt = (
                    _CONTINUATION_PROMPT
                    if is_continuation
                    else render_prompt(config.prompt_template, issue, attempt)
                )

                logger.info(
                    "issue_id=%s issue_identifier=%s turn=%d/%d starting",
                    issue.id, issue.identifier, turn_idx + 1, config.agent.max_turns,
                )

                with open(log_path, "a") as f:
                    f.write(f"\n{'=' * 60}\n")
                    f.write(f"Turn {turn_idx + 1} — {datetime.now(timezone.utc).isoformat()}\n")
                    f.write(f"{'=' * 60}\n\nPROMPT:\n{prompt}\n\nEVENTS:\n")

                result = await self._runner.run_turn(
                    workspace=workspace_path,
                    prompt=prompt,
                    is_continuation=is_continuation,
                    on_event=_log_event,
                )

                with open(log_path, "a") as f:
                    f.write(f"\nRESULT: success={result.success}\n")
                    if result.message:
                        f.write(f"MESSAGE: {result.message}\n")
                    if result.stderr:
                        f.write(f"STDERR:\n{result.stderr}\n")
                    if result.usage:
                        f.write(f"TOKENS: in={result.usage.input_tokens} out={result.usage.output_tokens}\n")

                if result.success:
                    logger.info(
                        "issue_id=%s issue_identifier=%s turn=%d succeeded",
                        issue.id, issue.identifier, turn_idx + 1,
                    )
                    break
                else:
                    raise RuntimeError(f"Turn {turn_idx + 1} failed: {result.message}")
        finally:
            await self._workspace.run_after_hook(issue)
