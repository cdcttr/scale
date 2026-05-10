from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from scale.agent.claude import ClaudeRunner
from scale.config.schema import WorkflowConfig
from scale.prompt.renderer import render_feedback_prompt
from scale.tracker.models import Issue
from scale.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


class FeedbackWorker:
    def __init__(self, workspace: WorkspaceManager, config: WorkflowConfig) -> None:
        self._workspace = workspace
        self._config = config
        self._runner = ClaudeRunner(config.codex)

    async def run(
        self,
        issue: Issue,
        pr_diff: str,
        pr_comments: list[dict],
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        workspace_path = await self._workspace.prepare(issue)
        branch_hook = f"git fetch origin {issue.branch_name} && git checkout {issue.branch_name}"
        await self._workspace.run_before_hook(issue, script_override=branch_hook)

        log_path = workspace_path / "agent.log"

        def _log_event(event: dict) -> None:
            with open(log_path, "a") as f:
                f.write(json.dumps(event) + "\n")
            if on_event:
                on_event(event)

        try:
            prompt = render_feedback_prompt(
                self._config.prompt_template,
                issue,
                pr_diff=pr_diff,
                pr_comments=pr_comments,
            )

            with open(log_path, "a") as f:
                f.write(f"\n{'=' * 60}\n")
                f.write(f"Feedback Turn — {datetime.now(timezone.utc).isoformat()}\n")
                f.write(f"{'=' * 60}\n\nPROMPT:\n{prompt}\n\nEVENTS:\n")

            result = await self._runner.run_turn(
                workspace=workspace_path,
                prompt=prompt,
                is_continuation=False,
                on_event=_log_event,
            )

            with open(log_path, "a") as f:
                f.write(f"\nRESULT: success={result.success}\n")
                if result.message:
                    f.write(f"MESSAGE: {result.message}\n")

            if not result.success:
                raise RuntimeError(f"Feedback turn failed: {result.message}")
        finally:
            await self._workspace.run_after_hook(issue)
