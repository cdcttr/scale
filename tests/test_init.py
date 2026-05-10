from __future__ import annotations

import sys
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from scale.agent.claude import TurnResult
from scale.config.schema import CodexConfig
from scale.init.runner import InitRunner, _detect_package_manager, _extract_code_blocks, _parse_github_repo


def _codex() -> CodexConfig:
    return CodexConfig()


def _turn_result(text: str, success: bool = True) -> TurnResult:
    return TurnResult(success=success, usage=None, message=text)


# --- _extract_code_blocks ---

def test_extract_code_blocks_yaml():
    text = "Here is the file:\n```yaml\nkey: value\n```\n"
    assert _extract_code_blocks(text, "yaml") == ["key: value"]


def test_extract_code_blocks_multiple():
    text = "```yaml\nfirst: block\n```\n\n```yaml\nsecond: block\n```"
    blocks = _extract_code_blocks(text, "yaml")
    assert len(blocks) == 2
    assert blocks[0] == "first: block"
    assert blocks[1] == "second: block"


def test_extract_code_blocks_no_match():
    assert _extract_code_blocks("No code blocks here", "yaml") == []


def test_extract_code_blocks_markdown_label():
    text = "```markdown\n# Title\ncontent\n```"
    assert _extract_code_blocks(text, "markdown") == ["# Title\ncontent"]


# --- _detect_package_manager ---

def test_detect_pyproject_toml(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    config_file, install_cmd = _detect_package_manager(tmp_path)
    assert config_file == "pyproject.toml"
    assert install_cmd == "uv sync"


def test_detect_package_json(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "test"}')
    config_file, install_cmd = _detect_package_manager(tmp_path)
    assert config_file == "package.json"
    assert install_cmd == "npm install"


def test_detect_cargo_toml(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'test'\n")
    config_file, install_cmd = _detect_package_manager(tmp_path)
    assert config_file == "Cargo.toml"
    assert install_cmd == "cargo build"


def test_detect_none(tmp_path: Path):
    config_file, install_cmd = _detect_package_manager(tmp_path)
    assert config_file is None
    assert install_cmd is None


def test_detect_pyproject_takes_priority_over_requirements(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    (tmp_path / "requirements.txt").write_text("requests\n")
    config_file, _ = _detect_package_manager(tmp_path)
    assert config_file == "pyproject.toml"


# --- _parse_github_repo ---

def test_parse_github_repo_https():
    assert _parse_github_repo("https://github.com/org/repo.git") == "org/repo"


def test_parse_github_repo_https_no_git_suffix():
    assert _parse_github_repo("https://github.com/org/repo") == "org/repo"


def test_parse_github_repo_ssh():
    assert _parse_github_repo("git@github.com:org/repo.git") == "org/repo"


def test_parse_github_repo_ssh_no_git_suffix():
    assert _parse_github_repo("git@github.com:org/repo") == "org/repo"


def test_parse_github_repo_invalid_exits():
    with pytest.raises(SystemExit):
        _parse_github_repo("https://gitlab.com/org/repo.git")


# --- InitRunner._check_overwrite ---

def test_check_overwrite_exits_when_file_exists(tmp_path: Path):
    (tmp_path / "WORKFLOW.md").write_text("existing")
    runner = InitRunner(_codex())
    with pytest.raises(SystemExit):
        runner._check_overwrite(tmp_path, force=False)


def test_check_overwrite_allows_force(tmp_path: Path):
    (tmp_path / "WORKFLOW.md").write_text("existing")
    runner = InitRunner(_codex())
    runner._check_overwrite(tmp_path, force=True)  # must not raise


def test_check_overwrite_allows_missing_file(tmp_path: Path):
    runner = InitRunner(_codex())
    runner._check_overwrite(tmp_path, force=False)  # must not raise


# --- InitRunner.gather_context ---

def test_gather_context_detects_python_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'myapp'\n")
    runner = InitRunner(_codex())
    with patch("scale.init.runner.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(stdout="https://github.com/org/repo.git\n", returncode=0),
            MagicMock(stdout="refs/remotes/origin/main\n", returncode=0),
        ]
        ctx = runner.gather_context(tmp_path)
    assert ctx["config_file"] == "pyproject.toml"
    assert ctx["install_cmd"] == "uv sync"
    assert ctx["repo"] == "org/repo"
    assert ctx["branch"] == "main"


def test_gather_context_detects_node_project(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "myapp"}')
    runner = InitRunner(_codex())
    with patch("scale.init.runner.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(stdout="https://github.com/org/node-app.git\n", returncode=0),
            MagicMock(stdout="refs/remotes/origin/main\n", returncode=0),
        ]
        ctx = runner.gather_context(tmp_path)
    assert ctx["config_file"] == "package.json"
    assert ctx["install_cmd"] == "npm install"
    assert ctx["repo"] == "org/node-app"


def test_gather_context_reads_readme(tmp_path: Path):
    (tmp_path / "README.md").write_text("\n".join(f"line {i}" for i in range(300)))
    runner = InitRunner(_codex())
    with patch("scale.init.runner.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(stdout="https://github.com/org/repo.git\n", returncode=0),
            MagicMock(stdout="refs/remotes/origin/main\n", returncode=0),
        ]
        ctx = runner.gather_context(tmp_path)
    lines = ctx["readme"].splitlines()
    assert len(lines) == 200


def test_gather_context_exits_when_no_git_remote(tmp_path: Path):
    import subprocess as sp
    runner = InitRunner(_codex())
    with patch("scale.init.runner.subprocess.run") as mock_run:
        mock_run.side_effect = sp.CalledProcessError(128, "git")
        with pytest.raises(SystemExit):
            runner.gather_context(tmp_path)


def test_gather_context_falls_back_to_main_branch(tmp_path: Path):
    import subprocess as sp
    runner = InitRunner(_codex())
    with patch("scale.init.runner.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(stdout="https://github.com/org/repo.git\n", returncode=0),
            sp.CalledProcessError(128, "git"),
        ]
        ctx = runner.gather_context(tmp_path)
    assert ctx["branch"] == "main"


# --- InitRunner.run: dry-run ---

@pytest.mark.asyncio
async def test_dry_run_does_not_write_files(tmp_path: Path, capsys):
    workflow_content = "---\ntracker:\n  repo: org/repo\n---\nPrompt here"
    claude_response = f"```yaml\n{workflow_content}\n```"

    runner = InitRunner(_codex())
    with patch.object(runner._runner, "run_turn", AsyncMock(return_value=_turn_result(claude_response))):
        with patch.object(runner, "gather_context", return_value={
            "repo": "org/repo", "branch": "main",
            "install_cmd": "uv sync", "config_file": "pyproject.toml",
            "config_content": "", "readme": "",
        }):
            await runner.run(tmp_path, dry_run=True)

    assert not (tmp_path / "WORKFLOW.md").exists()
    captured = capsys.readouterr()
    assert workflow_content in captured.out


# --- InitRunner.run: file writing ---

@pytest.mark.asyncio
async def test_writes_workflow_md(tmp_path: Path):
    workflow_content = "---\ntracker:\n  repo: org/repo\n---\nPrompt here"
    claude_response = f"```yaml\n{workflow_content}\n```"

    runner = InitRunner(_codex())
    with patch.object(runner._runner, "run_turn", AsyncMock(return_value=_turn_result(claude_response))):
        with patch.object(runner, "gather_context", return_value={
            "repo": "org/repo", "branch": "main",
            "install_cmd": "uv sync", "config_file": "pyproject.toml",
            "config_content": "", "readme": "",
        }):
            await runner.run(tmp_path)

    assert (tmp_path / "WORKFLOW.md").exists()
    assert (tmp_path / "WORKFLOW.md").read_text() == workflow_content


# --- InitRunner.run: --with-review ---

@pytest.mark.asyncio
async def test_with_review_writes_review_md(tmp_path: Path):
    workflow_content = "---\ntracker:\n  repo: org/repo\n---\nPrompt"
    review_content = "---\nreview:\n  model: claude-haiku\n---\nReview prompt"
    claude_response = (
        f"```yaml\n{workflow_content}\n```\n\n"
        f"```markdown\n{review_content}\n```"
    )

    runner = InitRunner(_codex())
    with patch.object(runner._runner, "run_turn", AsyncMock(return_value=_turn_result(claude_response))):
        with patch.object(runner, "gather_context", return_value={
            "repo": "org/repo", "branch": "main",
            "install_cmd": "uv sync", "config_file": "pyproject.toml",
            "config_content": "", "readme": "",
        }):
            await runner.run(tmp_path, with_review=True)

    assert (tmp_path / "WORKFLOW.md").exists()
    assert (tmp_path / "REVIEW.md").exists()
    assert (tmp_path / "REVIEW.md").read_text() == review_content


# --- InitRunner.run: overwrite guard ---

@pytest.mark.asyncio
async def test_run_refuses_overwrite_without_force(tmp_path: Path):
    (tmp_path / "WORKFLOW.md").write_text("existing")
    runner = InitRunner(_codex())
    with pytest.raises(SystemExit):
        await runner.run(tmp_path, force=False)


@pytest.mark.asyncio
async def test_run_overwrites_with_force(tmp_path: Path):
    (tmp_path / "WORKFLOW.md").write_text("existing")
    workflow_content = "---\ntracker:\n  repo: org/repo\n---\nPrompt"
    claude_response = f"```yaml\n{workflow_content}\n```"

    runner = InitRunner(_codex())
    with patch.object(runner._runner, "run_turn", AsyncMock(return_value=_turn_result(claude_response))):
        with patch.object(runner, "gather_context", return_value={
            "repo": "org/repo", "branch": "main",
            "install_cmd": "uv sync", "config_file": "pyproject.toml",
            "config_content": "", "readme": "",
        }):
            await runner.run(tmp_path, force=True)

    assert (tmp_path / "WORKFLOW.md").read_text() == workflow_content


# --- InitRunner.run: Claude failure ---

@pytest.mark.asyncio
async def test_run_exits_on_claude_failure(tmp_path: Path):
    runner = InitRunner(_codex())
    with patch.object(runner._runner, "run_turn", AsyncMock(return_value=_turn_result("crashed", success=False))):
        with patch.object(runner, "gather_context", return_value={
            "repo": "org/repo", "branch": "main",
            "install_cmd": "uv sync", "config_file": "pyproject.toml",
            "config_content": "", "readme": "",
        }):
            with pytest.raises(SystemExit):
                await runner.run(tmp_path)


@pytest.mark.asyncio
async def test_run_exits_when_no_yaml_block(tmp_path: Path):
    runner = InitRunner(_codex())
    with patch.object(runner._runner, "run_turn", AsyncMock(return_value=_turn_result("No code block here"))):
        with patch.object(runner, "gather_context", return_value={
            "repo": "org/repo", "branch": "main",
            "install_cmd": "uv sync", "config_file": "pyproject.toml",
            "config_content": "", "readme": "",
        }):
            with pytest.raises(SystemExit):
                await runner.run(tmp_path)
