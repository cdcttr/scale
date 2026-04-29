import pytest
from pathlib import Path
from unittest.mock import patch

from symphony.config.watcher import watch_workflow


def _write_workflow(path: Path) -> None:
    path.write_text(
        "---\ntracker:\n  repo: o/r\n  api_token: tok\n---\nDo the task.\n"
    )


@pytest.mark.asyncio
async def test_watch_workflow_calls_on_reload(tmp_path: Path):
    workflow = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow)

    reloaded = []

    async def _mock_awatch(*args, **kwargs):
        yield {("modified", str(workflow))}

    with patch("symphony.config.watcher.awatch", _mock_awatch):
        await watch_workflow(workflow, reloaded.append)

    assert len(reloaded) == 1
    assert reloaded[0].tracker.repo == "o/r"


@pytest.mark.asyncio
async def test_watch_workflow_keeps_last_config_on_error(tmp_path: Path):
    workflow = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow)

    reloaded = []

    async def _mock_awatch(*args, **kwargs):
        yield {("modified", str(workflow))}

    with patch("symphony.config.watcher.awatch", _mock_awatch):
        with patch(
            "symphony.config.watcher.load_workflow",
            side_effect=ValueError("bad yaml"),
        ):
            await watch_workflow(workflow, reloaded.append)

    assert len(reloaded) == 0  # on_reload never called when load fails


@pytest.mark.asyncio
async def test_watch_workflow_multiple_changes(tmp_path: Path):
    workflow = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow)

    reloaded = []
    change = {("modified", str(workflow))}

    async def _mock_awatch(*args, **kwargs):
        yield change
        yield change
        yield change

    with patch("symphony.config.watcher.awatch", _mock_awatch):
        await watch_workflow(workflow, reloaded.append)

    assert len(reloaded) == 3
