import pytest
from pathlib import Path
from datetime import datetime
from symphony.workspace.manager import WorkspaceManager, sanitize_identifier
from symphony.tracker.models import Issue
from symphony.config.schema import WorkflowConfig, TrackerConfig, WorkspaceConfig

def _config(root: str) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="owner/repo", api_token="tok"),
        workspace=WorkspaceConfig(root=root),
    )

def _issue(identifier="owner/repo#42") -> Issue:
    return Issue(
        id="n42", identifier=identifier, number=42,
        title="Fix bug", description="", state="active",
        labels=[], branch_name="symphony/42-fix-bug",
        url="https://github.com/owner/repo/issues/42",
        priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2),
    )

def test_sanitize_identifier_replaces_special_chars():
    assert sanitize_identifier("owner/repo#42") == "owner_repo_42"

def test_sanitize_identifier_allows_safe_chars():
    assert sanitize_identifier("my-issue.1") == "my-issue.1"

@pytest.mark.asyncio
async def test_prepare_creates_directory(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path)))
    path = await mgr.prepare(_issue(), hooks_enabled=False)
    assert path.exists()
    assert path.is_dir()

@pytest.mark.asyncio
async def test_prepare_reuses_existing_directory(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path)))
    path1 = await mgr.prepare(_issue(), hooks_enabled=False)
    (path1 / "sentinel.txt").write_text("hello")
    path2 = await mgr.prepare(_issue(), hooks_enabled=False)
    assert path1 == path2
    assert (path2 / "sentinel.txt").exists()

@pytest.mark.asyncio
async def test_prepare_path_stays_within_root(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path)))
    path = await mgr.prepare(_issue(), hooks_enabled=False)
    assert str(path).startswith(str(tmp_path))

@pytest.mark.asyncio
async def test_remove_deletes_directory(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path)))
    path = await mgr.prepare(_issue(), hooks_enabled=False)
    assert path.exists()
    await mgr.remove(_issue(), hooks_enabled=False)
    assert not path.exists()
