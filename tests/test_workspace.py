import pytest
from pathlib import Path
from datetime import datetime
from scale.workspace.manager import WorkspaceManager, sanitize_identifier
from scale.tracker.models import Issue
from scale.config.schema import WorkflowConfig, TrackerConfig, WorkspaceConfig, HooksConfig

def _config(root: str, **hook_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="owner/repo", api_token="tok"),
        workspace=WorkspaceConfig(root=root),
        hooks=HooksConfig(**hook_kwargs) if hook_kwargs else HooksConfig(),
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


# --- hook execution ---

@pytest.mark.asyncio
async def test_after_create_hook_runs_on_new_dir(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path), after_create="touch after_create_ran"))
    path = await mgr.prepare(_issue())
    assert (path / "after_create_ran").exists()


@pytest.mark.asyncio
async def test_after_create_hook_not_run_on_existing_dir(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path), after_create="touch after_create_ran"))
    # Create dir first without hook, then run again
    await mgr.prepare(_issue(), hooks_enabled=False)
    sentinel = mgr._path(_issue()) / "after_create_ran"
    sentinel.unlink(missing_ok=True)
    await mgr.prepare(_issue())
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_before_run_hook_runs(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path), before_run="touch before_run_ran"))
    await mgr.prepare(_issue(), hooks_enabled=False)
    await mgr.run_before_hook(_issue())
    assert (mgr._path(_issue()) / "before_run_ran").exists()


@pytest.mark.asyncio
async def test_after_run_hook_runs(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path), after_run="touch after_run_ran"))
    await mgr.prepare(_issue(), hooks_enabled=False)
    await mgr.run_after_hook(_issue())
    assert (mgr._path(_issue()) / "after_run_ran").exists()


@pytest.mark.asyncio
async def test_after_run_hook_failure_not_raised(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path), after_run="exit 1"))
    await mgr.prepare(_issue(), hooks_enabled=False)
    await mgr.run_after_hook(_issue())  # must not raise


@pytest.mark.asyncio
async def test_before_remove_hook_runs(tmp_path):
    sentinel = tmp_path / "before_remove_ran"
    mgr = WorkspaceManager(_config(str(tmp_path), before_remove=f"touch {sentinel}"))
    await mgr.prepare(_issue(), hooks_enabled=False)
    await mgr.remove(_issue())
    assert sentinel.exists()


@pytest.mark.asyncio
async def test_before_remove_hook_failure_not_raised(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path), before_remove="exit 1"))
    await mgr.prepare(_issue(), hooks_enabled=False)
    await mgr.remove(_issue())  # must not raise


@pytest.mark.asyncio
async def test_hook_nonzero_exit_raises(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path), before_run="exit 42"))
    await mgr.prepare(_issue(), hooks_enabled=False)
    with pytest.raises(RuntimeError, match="exit 42"):
        await mgr.run_before_hook(_issue())


@pytest.mark.asyncio
async def test_hook_timeout_raises(tmp_path):
    mgr = WorkspaceManager(_config(str(tmp_path), before_run="sleep 10", timeout_ms=100))
    await mgr.prepare(_issue(), hooks_enabled=False)
    with pytest.raises(RuntimeError, match="timed out"):
        await mgr.run_before_hook(_issue())
