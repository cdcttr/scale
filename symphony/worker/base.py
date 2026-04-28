from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable, Optional

from symphony.config.schema import WorkflowConfig
from symphony.tracker.models import Issue


class Worker(ABC):
    @abstractmethod
    async def run(
        self,
        issue: Issue,
        config: WorkflowConfig,
        attempt: Optional[int],
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        ...
