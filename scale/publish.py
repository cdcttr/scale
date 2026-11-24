"""Orchestrator-side publishing (agent.publish: orchestrator).

The agent only commits inside its workspace. After a successful run, Scale:
  1. refuses to treat the run as done if there are no new commits (honest completion),
  2. pushes the branch with its own credential, supplied per command via the
     environment (never in argv, the remote URL, or .git/config),
  3. opens the pull request, or reuses the one already open for the branch.

This keeps git and tracker tokens out of the agent's reach entirely, so a
sandboxed agent cannot push to main or anywhere else: there is nothing to push with.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from scale.config.schema import WorkflowConfig
from scale.scm.base import SCMClient
from scale.tracker.models import Issue

logger = logging.getLogger(__name__)

PUBLISH_OVERRIDE = (
    "\n\n## Publishing (overrides any instruction above about pushing or pull requests)\n\n"
    "Commit your work to the current git branch when you are done. "
    "Do not push, and do not open a pull request: Scale publishes your commits "
    "and opens the pull request itself. You have no credentials to push with. "
    "If you cannot finish, say so plainly in your final message instead of committing partial work as done."
)

_HELPER = '!f() { echo username=x-access-token; echo "password=$SCALE_GIT_TOKEN"; }; f'


class NoChangesError(RuntimeError):
    """The agent finished a run without committing anything new."""


class Publisher:
    def __init__(self, config: WorkflowConfig, scm: SCMClient) -> None:
        self._config = config
        self._scm = scm

    def _git_auth(self) -> tuple[list[str], dict[str, str]]:
        """Per-command credential: helper reads the token from the environment."""
        args = ["-c", "credential.helper=", "-c", f"credential.helper={_HELPER}"]
        env = {**os.environ, "SCALE_GIT_TOKEN": self._config.tracker.api_token,
               "GIT_TERMINAL_PROMPT": "0"}
        return args, env

    async def _git(self, cwd: Path, *args: str, auth: bool = False) -> str:
        auth_args, env = self._git_auth() if auth else ([], {**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        cmd = ["git", *auth_args, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            # Report the command without auth args (they carry no secret, but stay terse).
            raise subprocess.CalledProcessError(proc.returncode, ["git", *args], out, err)
        return out.decode(errors="replace").strip()

    async def commits_ahead(self, workspace: Path) -> int:
        base = self._config.tracker.default_branch
        await self._git(workspace, "fetch", "--quiet", "origin", base, auth=True)
        n = await self._git(workspace, "rev-list", "--count", f"origin/{base}..HEAD")
        return int(n or 0)

    async def publish(self, issue: Issue, workspace: Path, *, force: bool = False) -> Optional[dict]:
        ahead = await self.commits_ahead(workspace)
        if ahead == 0:
            raise NoChangesError(
                f"{issue.identifier}: run reported success but produced no new commits"
            )
        branch = issue.branch_name
        push = ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"]
        if force:
            push.insert(1, f"--force-with-lease=refs/heads/{branch}")
        await self._git(workspace, *push, auth=True)
        logger.info("issue_identifier=%s published %d commit(s) to %s", issue.identifier, ahead, branch)

        pr = await self._scm.fetch_pr_for_branch(branch)
        if pr is not None:
            return pr
        return await self._scm.create_pr(
            head=branch,
            base=self._config.tracker.default_branch,
            title=issue.title,
            body=(
                f"Closes #{issue.number}\n\n"
                f"Implements {issue.identifier}: {issue.url}\n\n"
                "_Published by Scale from the agent's commits (publish: orchestrator)._"
            ),
        )


def apply_publish_mode(prompt: str, config: WorkflowConfig) -> str:
    """In orchestrator mode, tell the agent to commit only (no push, no PR)."""
    if config.agent.publish != "orchestrator":
        return prompt
    prompt = prompt.replace("Open a pull request when done.", "Commit your work when done.")
    return prompt + PUBLISH_OVERRIDE
