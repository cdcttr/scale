from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceState:
    uncommitted_files: int
    commits_since_start: int
    status_summary: str

    @property
    def has_progress(self) -> bool:
        return self.uncommitted_files > 0 or self.commits_since_start > 0


async def _run_git(args: list[str], cwd: Path) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return ""
        return stdout.decode(errors="replace").strip()
    except Exception:
        return ""


async def get_head_sha(workspace: Path) -> Optional[str]:
    result = await _run_git(["rev-parse", "HEAD"], workspace)
    return result or None


async def gather_workspace_state(workspace: Path, since_sha: Optional[str] = None) -> WorkspaceState:
    status_output = await _run_git(["status", "--short"], workspace)
    uncommitted_files = len([ln for ln in status_output.splitlines() if ln.strip()])

    if since_sha:
        log_output = await _run_git(["log", "--oneline", f"{since_sha}..HEAD"], workspace)
        commits_since_start = len([ln for ln in log_output.splitlines() if ln.strip()])
    else:
        commits_since_start = 0

    return WorkspaceState(
        uncommitted_files=uncommitted_files,
        commits_since_start=commits_since_start,
        status_summary=status_output,
    )
