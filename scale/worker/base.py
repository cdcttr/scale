from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable, Optional

from scale.config.schema import WorkflowConfig
from scale.tracker.models import Issue


class Worker(ABC):
    @abstractmethod
    async def run(
        self,
        issue: Issue,
        config: WorkflowConfig,
        attempt: Optional[int],
        on_event: Optional[Callable[[dict], None]] = None,
        previous_attempt_summary: Optional[str] = None,
    ) -> None:
        ...
