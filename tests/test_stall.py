from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from scale.agent.stall import WorkspaceState, gather_workspace_state, get_head_sha


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True, capture_output=True)


def _git_commit(path: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", message],
        check=True, capture_output=True,
    )


def _head_sha(path: Path) -> Optional[str]:
    r = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _git_init(tmp_path)
    _git_commit(tmp_path, "initial commit")
    return tmp_path


@pytest.mark.asyncio
async def test_clean_workspace_has_no_progress(git_repo: Path) -> None:
    sha = _head_sha(git_repo)
    state = await gather_workspace_state(git_repo, since_sha=sha)
    assert state.uncommitted_files == 0
    assert state.commits_since_start == 0
    assert not state.has_progress


@pytest.mark.asyncio
async def test_untracked_file_counts_as_progress(git_repo: Path) -> None:
    sha = _head_sha(git_repo)
    (git_repo / "work.py").write_text("x = 1")
    state = await gather_workspace_state(git_repo, since_sha=sha)
    assert state.uncommitted_files > 0
    assert state.has_progress


@pytest.mark.asyncio
async def test_staged_file_counts_as_progress(git_repo: Path) -> None:
    sha = _head_sha(git_repo)
    (git_repo / "staged.py").write_text("y = 2")
    subprocess.run(["git", "-C", str(git_repo), "add", "staged.py"], check=True, capture_output=True)
    state = await gather_workspace_state(git_repo, since_sha=sha)
    assert state.uncommitted_files > 0
    assert state.has_progress


@pytest.mark.asyncio
async def test_modified_tracked_file_counts_as_progress(git_repo: Path) -> None:
    f = git_repo / "tracked.py"
    f.write_text("original")
    _git_commit(git_repo, "add tracked.py")
    sha = _head_sha(git_repo)
    f.write_text("modified")
    state = await gather_workspace_state(git_repo, since_sha=sha)
    assert state.uncommitted_files > 0
    assert state.has_progress


@pytest.mark.asyncio
async def test_recent_commits_count_as_progress(git_repo: Path) -> None:
    sha = _head_sha(git_repo)
    _git_commit(git_repo, "work done after start")
    state = await gather_workspace_state(git_repo, since_sha=sha)
    assert state.commits_since_start > 0
    assert state.has_progress


@pytest.mark.asyncio
async def test_commits_before_since_sha_are_not_counted(git_repo: Path) -> None:
    _git_commit(git_repo, "old work")
    sha = _head_sha(git_repo)
    state = await gather_workspace_state(git_repo, since_sha=sha)
    assert state.commits_since_start == 0


@pytest.mark.asyncio
async def test_non_git_dir_returns_zero_state(tmp_path: Path) -> None:
    state = await gather_workspace_state(tmp_path, since_sha=None)
    assert state.uncommitted_files == 0
    assert state.commits_since_start == 0
    assert not state.has_progress


@pytest.mark.asyncio
async def test_status_summary_non_empty_when_changes(git_repo: Path) -> None:
    (git_repo / "new.py").write_text("z = 3")
    state = await gather_workspace_state(git_repo, since_sha=None)
    assert state.status_summary != ""


@pytest.mark.asyncio
async def test_workspace_state_has_progress_property() -> None:
    assert not WorkspaceState(uncommitted_files=0, commits_since_start=0, status_summary="").has_progress
    assert WorkspaceState(uncommitted_files=1, commits_since_start=0, status_summary="M foo.py").has_progress
    assert WorkspaceState(uncommitted_files=0, commits_since_start=2, status_summary="").has_progress


@pytest.mark.asyncio
async def test_get_head_sha_in_repo(git_repo: Path) -> None:
    sha = await get_head_sha(git_repo)
    assert sha is not None
    assert len(sha) == 40


@pytest.mark.asyncio
async def test_get_head_sha_in_non_repo(tmp_path: Path) -> None:
    sha = await get_head_sha(tmp_path)
    assert sha is None
