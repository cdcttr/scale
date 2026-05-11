from __future__ import annotations
import logging
import tempfile
from pathlib import Path
from typing import Callable, Optional

from scale.agent.claude import ClaudeRunner, TurnResult
from scale.config.schema import WorkflowConfig
from scale.prompt.renderer import render_review_prompt
from scale.tracker.models import Issue

logger = logging.getLogger(__name__)


class ReviewWorker:
    def __init__(self, config: WorkflowConfig) -> None:
        self._config = config
        self._runner = ClaudeRunner(config.codex)

    async def run(
        self,
        issue: Issue,
        pr_number: int,
        pr_url: str,
        pr_diff: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> "TurnResult":
        assert self._config.review is not None
        review = self._config.review

        prompt = render_review_prompt(
            review.template,
            issue,
            pr_number=pr_number,
            pr_url=pr_url,
            pr_diff=pr_diff,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await self._runner.run_turn(
                workspace=Path(tmpdir),
                prompt=prompt,
                is_continuation=False,
                on_event=on_event,
                model=review.model,
            )

        if not result.success:
            raise RuntimeError(f"Review failed: {result.message}")
        return result
