from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from scale.agent.claude import ClaudeRunner
from scale.config.schema import WorkflowConfig
from scale.prompt.renderer import render_rebase_prompt
from scale.tracker.github import GitHubClient
from scale.tracker.models import Issue
from scale.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


class RebaseWorker:
    def __init__(
        self,
        workspace: WorkspaceManager,
        github: GitHubClient,
        config: WorkflowConfig,
    ) -> None:
        self._workspace = workspace
        self._github = github
        self._config = config
        assert config.rebase is not None
        self._runner = ClaudeRunner(config.codex)

    async def run(
        self,
        issue: Issue,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> bool:
        assert self._config.rebase is not None
        rebase_cfg = self._config.rebase

        workspace_path = await self._workspace.prepare(issue)
        script = f"git fetch origin && git checkout {issue.branch_name}"
        await self._workspace.run_before_hook(issue, script_override=script)

        pr = await self._github.fetch_pr_for_branch(issue.branch_name)
        if pr is None:
            logger.warning("No open PR for issue #%d, skipping rebase", issue.number)
            return False

        pr_diff = await self._github.fetch_pr_diff(pr["number"])
        conflict_context = await self._github.fetch_conflict_context(issue.branch_name)

        log_path = workspace_path / "rebase.log"

        def _log_event(event: dict) -> None:
            with open(log_path, "a") as f:
                f.write(json.dumps(event) + "\n")
            if on_event:
                on_event(event)

        prompt = render_rebase_prompt(
            rebase_cfg.template,
            issue,
            pr_number=pr["number"],
            pr_url=pr["html_url"],
            pr_diff=pr_diff,
            conflict_context=conflict_context,
        )

        with open(log_path, "a") as f:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"Rebase Turn — {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"{'=' * 60}\n\nPROMPT:\n{prompt}\n\nEVENTS:\n")

        try:
            result = await self._runner.run_turn(
                workspace=workspace_path,
                prompt=prompt,
                is_continuation=False,
                on_event=_log_event,
                model=rebase_cfg.model,
            )

            with open(log_path, "a") as f:
                f.write(f"\nRESULT: success={result.success}\n")

            if not result.success:
                logger.warning(
                    "Rebase agent failed for issue #%d: %s", issue.number, result.message
                )
                return False

            return True
        finally:
            await self._workspace.run_after_hook(issue)
