from __future__ import annotations
import asyncio
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from scale.config.schema import WorkflowConfig
from scale.tracker.models import Issue

logger = logging.getLogger(__name__)

_UNSAFE_RE = re.compile(r'[^A-Za-z0-9._-]')


def sanitize_identifier(identifier: str) -> str:
    return _UNSAFE_RE.sub('_', identifier)


class WorkspaceManager:
    def __init__(self, config: WorkflowConfig) -> None:
        self._root = Path(config.workspace.root)
        self._hooks = config.hooks
        self._log_archive = Path(config.workspace.log_archive) if config.workspace.log_archive else None

    def _path(self, issue: Issue) -> Path:
        name = sanitize_identifier(issue.identifier)
        path = (self._root / name).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"Workspace path escapes root: {path}")
        return path

    async def _run_hook(self, script: str, cwd: Path) -> None:
        if not script:
            return
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(
                proc.communicate(),
                timeout=self._hooks.timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"Hook timed out: {script!r}")
        if proc.returncode != 0:
            raise RuntimeError(f"Hook failed (exit {proc.returncode}): {script!r}")

    async def prepare(self, issue: Issue, hooks_enabled: bool = True) -> Path:
        path = self._path(issue)
        created_now = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if created_now and hooks_enabled and self._hooks.after_create:
            await self._run_hook(self._hooks.after_create, path)
        return path

    async def run_before_hook(self, issue: Issue) -> None:
        path = self._path(issue)
        if self._hooks.before_run:
            await self._run_hook(self._hooks.before_run, path)

    async def run_after_hook(self, issue: Issue) -> None:
        path = self._path(issue)
        if self._hooks.after_run:
            try:
                await self._run_hook(self._hooks.after_run, path)
            except Exception as e:
                logger.warning("after_run hook failed (ignored): %s", e)

    async def remove(self, issue: Issue, hooks_enabled: bool = True) -> None:
        path = self._path(issue)
        if not path.exists():
            return
        if hooks_enabled and self._hooks.before_remove:
            try:
                await self._run_hook(self._hooks.before_remove, path)
            except Exception as e:
                logger.warning("before_remove hook failed (ignored): %s", e)
        if self._log_archive is not None:
            log_file = path / "agent.log"
            if log_file.exists():
                self._log_archive.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                dest = self._log_archive / f"{issue.number}-{timestamp}.log"
                shutil.copy2(log_file, dest)
        shutil.rmtree(path, ignore_errors=True)
