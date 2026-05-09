from __future__ import annotations
import logging
from pathlib import Path
from typing import Callable

from watchfiles import awatch

from scale.config.loader import load_workflow
from scale.config.schema import WorkflowConfig

logger = logging.getLogger(__name__)


async def watch_workflow(
    path: Path,
    on_reload: Callable[[WorkflowConfig], None],
) -> None:
    async for _ in awatch(str(path)):
        try:
            new_config = load_workflow(path)
            on_reload(new_config)
            logger.info("WORKFLOW.md reloaded successfully")
        except Exception as e:
            logger.error("WORKFLOW.md reload failed, keeping last config: %s", e)
