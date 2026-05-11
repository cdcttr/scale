from __future__ import annotations
import logging
from typing import Callable, Optional

from scale.config.schema import WorkflowConfig
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

    async def run(
        self,
        issue: Issue,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> bool:
        raise NotImplementedError
