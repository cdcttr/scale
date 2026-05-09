from __future__ import annotations
import re
import shutil
import logging
from pathlib import Path

from scale.config.schema import WorkflowConfig
from scale.tracker.github import GitHubClient
from scale.workspace.manager import WorkspaceManager, sanitize_identifier

logger = logging.getLogger(__name__)


def _parse_issue_number(dir_name: str, repo_prefix: str) -> int | None:
    m = re.fullmatch(rf"{re.escape(repo_prefix)}_(\d+)", dir_name)
    return int(m.group(1)) if m else None


async def clean(
    config: WorkflowConfig,
    dry_run: bool = False,
    all_workspaces: bool = False,
    yes: bool = False,
) -> None:
    workspace_root = Path(config.workspace.root)

    if not workspace_root.exists():
        print("workspaces/ not found — nothing to clean.")
        return

    subdirs = sorted(p for p in workspace_root.iterdir() if p.is_dir())

    if not subdirs:
        print("No workspaces found.")
        return

    if all_workspaces:
        if not yes and not dry_run:
            ans = input(f"Remove all {len(subdirs)} workspace(s)? [y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return
        for path in subdirs:
            if dry_run:
                print(f"Would remove: {path}")
            else:
                print(f"Removing: {path}")
                shutil.rmtree(path, ignore_errors=True)
        return

    repo_prefix = sanitize_identifier(config.tracker.repo)
    numbers_to_dirs: dict[int, Path] = {}
    for d in subdirs:
        n = _parse_issue_number(d.name, repo_prefix)
        if n is not None:
            numbers_to_dirs[n] = d

    if not numbers_to_dirs:
        print("No workspaces matched the current repository — nothing to clean.")
        return

    tracker = GitHubClient(config.tracker)
    issues = await tracker.fetch_issues_by_numbers(list(numbers_to_dirs.keys()))

    manager = WorkspaceManager(config)
    removed = 0
    for issue in issues:
        if issue.state == "terminal":
            path = numbers_to_dirs[issue.number]
            if dry_run:
                print(f"Would remove: {path}")
            else:
                print(f"Removing: {path}")
                await manager.remove(issue)
            removed += 1

    if removed == 0:
        print("No stale workspaces to remove.")
