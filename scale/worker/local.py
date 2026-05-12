from __future__ import annotations
import logging
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
        previous_attempt_summary: Optional[str] = None,
    ) -> None:
        workspace_path = await self._workspace.prepare(issue)
        await self._workspace.run_before_hook(issue)

        log_path = workspace_path / "agent.log"

        try:
            for turn_idx in range(config.agent.max_turns):
                is_continuation = turn_idx > 0
                prompt = (
                    _CONTINUATION_PROMPT
                    if is_continuation
                    else render_prompt(
                        config.prompt_template, issue, attempt, previous_attempt_summary
                    )
                )

                logger.info(
                    "issue_id=%s issue_identifier=%s turn=%d/%d starting",
                    issue.id, issue.identifier, turn_idx + 1, config.agent.max_turns,
                )

                result = await self._runner.run_turn(
                    workspace=workspace_path,
                    prompt=prompt,
                    is_continuation=is_continuation,
                    on_event=on_event,
                    log_path=log_path,
                    log_label=f"Turn {turn_idx + 1}",
                )

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
