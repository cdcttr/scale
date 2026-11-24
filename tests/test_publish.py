"""Orchestrator-side publishing: the agent only commits; Scale pushes and opens the PR.

Uses real git: a workspace clone plus a local bare repo standing in for origin.
"""
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scale.config.schema import AgentConfig, TrackerConfig, WorkflowConfig
from scale.publish import NoChangesError, Publisher, PUBLISH_OVERRIDE
from scale.tracker.models import Issue

TOKEN = "tok-SECRET-123"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
             "HOME": str(cwd)},
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    ws = tmp_path / "ws"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "README").write_text("hi\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")
    _git(tmp_path, "clone", str(origin), str(ws))
    return ws, origin


def _config() -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token=TOKEN),
        agent=AgentConfig(publish="orchestrator"),
        prompt_template="Work on {{ issue.title }}.",
    )


def _issue() -> Issue:
    return Issue(
        id="i1", identifier="o/r#7", number=7, title="Add hunt tests", description="d",
        state="active", labels=[], branch_name="scale/7", url="https://example.com/7",
        priority=None, created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


def _scm(existing_pr=None) -> AsyncMock:
    scm = AsyncMock()
    scm.fetch_pr_for_branch = AsyncMock(return_value=existing_pr)
    scm.create_pr = AsyncMock(return_value={"number": 11, "html_url": "https://pr/11"})
    return scm


def _agent_commits(ws: Path, name: str = "work.txt", text: str = "done\n") -> None:
    (ws / name).write_text(text)
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", f"agent: {name}")


def _remote_ref(origin: Path, branch: str) -> str:
    return _git(origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")


@pytest.mark.asyncio
async def test_no_commits_is_not_done(repo):
    ws, origin = repo
    scm = _scm()
    with pytest.raises(NoChangesError):
        await Publisher(_config(), scm).publish(_issue(), ws)
    scm.create_pr.assert_not_called()
    with pytest.raises(subprocess.CalledProcessError):
        _remote_ref(origin, "scale/7")


@pytest.mark.asyncio
async def test_pushes_branch_and_opens_pr(repo):
    ws, origin = repo
    _agent_commits(ws)
    scm = _scm()
    pr = await Publisher(_config(), scm).publish(_issue(), ws)
    assert _remote_ref(origin, "scale/7") == _git(ws, "rev-parse", "HEAD")
    assert _git(origin, "rev-parse", "main") != _git(ws, "rev-parse", "HEAD")  # main untouched
    scm.create_pr.assert_awaited_once()
    kw = scm.create_pr.await_args.kwargs
    assert kw["head"] == "scale/7" and kw["base"] == "main"
    assert "Closes #7" in kw["body"]
    assert kw["title"] == "Add hunt tests"
    assert pr["number"] == 11


@pytest.mark.asyncio
async def test_existing_pr_is_reused(repo):
    ws, _ = repo
    _agent_commits(ws)
    scm = _scm(existing_pr={"number": 5, "html_url": "https://pr/5"})
    pr = await Publisher(_config(), scm).publish(_issue(), ws)
    scm.create_pr.assert_not_called()
    assert pr["number"] == 5


@pytest.mark.asyncio
async def test_token_never_lands_in_workspace(repo):
    ws, _ = repo
    _agent_commits(ws)
    await Publisher(_config(), _scm()).publish(_issue(), ws)
    for f in (ws / ".git").rglob("*"):
        if f.is_file():
            assert TOKEN.encode() not in f.read_bytes(), f"token leaked into {f}"


def test_auth_is_env_not_argv():
    args, env = Publisher(_config(), _scm())._git_auth()
    assert TOKEN not in " ".join(args)
    assert env["SCALE_GIT_TOKEN"] == TOKEN
    assert any(a.startswith("credential.helper=!") for a in args)
    assert "credential.helper=" in args  # clears inherited helpers first


@pytest.mark.asyncio
async def test_rebase_publish_force_with_lease(repo):
    ws, origin = repo
    _agent_commits(ws)
    pub = Publisher(_config(), _scm())
    await pub.publish(_issue(), ws)
    # Agent rewrites history (as a rebase would), then Scale republishes.
    _git(ws, "commit", "--amend", "-m", "agent: rebased")
    with pytest.raises(subprocess.CalledProcessError):
        await pub.publish(_issue(), ws)  # plain push refuses a rewrite
    await pub.publish(_issue(), ws, force=True)
    assert _remote_ref(origin, "scale/7") == _git(ws, "rev-parse", "HEAD")


def test_override_text_forbids_push_and_pr():
    assert "Do not push" in PUBLISH_OVERRIDE and "pull request" in PUBLISH_OVERRIDE


def test_publish_mode_config():
    assert AgentConfig().publish == "agent"
    assert AgentConfig(publish="orchestrator").publish == "orchestrator"
    with pytest.raises(Exception):
        AgentConfig(publish="bogus")


@pytest.mark.asyncio
async def test_github_create_pr_request():
    import json as _json
    import httpx
    import respx
    from scale.tracker.github import GitHubClient
    with respx.mock:
        route = respx.post("https://api.github.com/repos/owner/repo/pulls").mock(
            return_value=httpx.Response(201, json={"number": 9, "html_url": "https://github.com/owner/repo/pull/9"})
        )
        gh = GitHubClient(TrackerConfig(repo="owner/repo", api_token=TOKEN))
        pr = await gh.create_pr(head="scale/7", base="main", title="T", body="Closes #7")
    sent = _json.loads(route.calls[0].request.content)
    assert sent == {"head": "scale/7", "base": "main", "title": "T", "body": "Closes #7"}
    assert pr["number"] == 9
