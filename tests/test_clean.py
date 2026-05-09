from __future__ import annotations
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scale.config.schema import WorkflowConfig, TrackerConfig, WorkspaceConfig
from scale.tracker.models import Issue


def _config(root: str) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="owner/repo", api_token="tok"),
        workspace=WorkspaceConfig(root=root),
    )


def _issue(number: int, state: str = "terminal") -> Issue:
    return Issue(
        id=f"n{number}",
        identifier=f"owner/repo#{number}",
        number=number,
        title=f"Issue {number}",
        description="",
        state=state,
        labels=[],
        branch_name=f"symphony/{number}-issue-{number}",
        url=f"https://github.com/owner/repo/issues/{number}",
        priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2),
    )


# --- _parse_issue_number ---


def test_parse_issue_number_basic():
    from scale.clean import _parse_issue_number
    assert _parse_issue_number("owner_repo_42", "owner_repo") == 42


def test_parse_issue_number_no_match_wrong_prefix():
    from scale.clean import _parse_issue_number
    assert _parse_issue_number("other_repo_42", "owner_repo") is None


def test_parse_issue_number_no_match_trailing_extra():
    from scale.clean import _parse_issue_number
    assert _parse_issue_number("owner_repo_42_extra", "owner_repo") is None


def test_parse_issue_number_no_match_non_numeric():
    from scale.clean import _parse_issue_number
    assert _parse_issue_number("owner_repo_abc", "owner_repo") is None


# --- clean: empty / missing ---


async def test_clean_missing_workspaces_dir(tmp_path, capsys):
    from scale.clean import clean
    config = _config(str(tmp_path / "workspaces"))
    await clean(config)
    assert "not found" in capsys.readouterr().out


async def test_clean_empty_workspaces_dir(tmp_path, capsys):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    config = _config(str(ws))
    await clean(config)
    assert "No workspaces found" in capsys.readouterr().out


# --- clean: default (terminal issues) ---


async def test_clean_removes_terminal_workspace(tmp_path):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / "owner_repo_42").mkdir()

    config = _config(str(ws))
    with patch("scale.clean.GitHubClient") as MockClient:
        MockClient.return_value.fetch_issues_by_numbers = AsyncMock(
            return_value=[_issue(42, "terminal")]
        )
        await clean(config)

    assert not (ws / "owner_repo_42").exists()


async def test_clean_skips_active_workspace(tmp_path, capsys):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / "owner_repo_42").mkdir()

    config = _config(str(ws))
    with patch("scale.clean.GitHubClient") as MockClient:
        MockClient.return_value.fetch_issues_by_numbers = AsyncMock(
            return_value=[_issue(42, "active")]
        )
        await clean(config)

    assert (ws / "owner_repo_42").exists()
    assert "No stale" in capsys.readouterr().out


async def test_clean_skips_unrelated_dirs(tmp_path, capsys):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / "other_org_repo_42").mkdir()

    config = _config(str(ws))
    await clean(config)

    assert (ws / "other_org_repo_42").exists()
    out = capsys.readouterr().out
    assert "No workspaces matched" in out or "nothing to clean" in out


# --- clean: --dry-run ---


async def test_clean_dry_run_does_not_delete(tmp_path, capsys):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / "owner_repo_42").mkdir()

    config = _config(str(ws))
    with patch("scale.clean.GitHubClient") as MockClient:
        MockClient.return_value.fetch_issues_by_numbers = AsyncMock(
            return_value=[_issue(42, "terminal")]
        )
        await clean(config, dry_run=True)

    assert (ws / "owner_repo_42").exists()
    assert "Would remove" in capsys.readouterr().out


# --- clean: --all ---


async def test_clean_all_yes_removes_everything(tmp_path, capsys):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / "owner_repo_42").mkdir()
    (ws / "owner_repo_99").mkdir()

    config = _config(str(ws))
    await clean(config, all_workspaces=True, yes=True)

    assert not (ws / "owner_repo_42").exists()
    assert not (ws / "owner_repo_99").exists()
    assert "Removing" in capsys.readouterr().out


async def test_clean_all_dry_run_does_not_delete(tmp_path, capsys):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / "owner_repo_42").mkdir()

    config = _config(str(ws))
    await clean(config, all_workspaces=True, dry_run=True)

    assert (ws / "owner_repo_42").exists()
    assert "Would remove" in capsys.readouterr().out


async def test_clean_all_prompts_confirm_yes(tmp_path, monkeypatch):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / "owner_repo_42").mkdir()

    config = _config(str(ws))
    monkeypatch.setattr("builtins.input", lambda _: "y")
    await clean(config, all_workspaces=True, yes=False)

    assert not (ws / "owner_repo_42").exists()


async def test_clean_all_prompts_confirm_no_aborts(tmp_path, capsys, monkeypatch):
    from scale.clean import clean
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / "owner_repo_42").mkdir()

    config = _config(str(ws))
    monkeypatch.setattr("builtins.input", lambda _: "n")
    await clean(config, all_workspaces=True, yes=False)

    assert (ws / "owner_repo_42").exists()
    assert "Aborted" in capsys.readouterr().out
