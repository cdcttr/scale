"""BeadsTracker against a real, throwaway beads database (bd must be on PATH)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from scale.config.schema import AgentConfig, TrackerConfig, WorkflowConfig
from scale.publish import Publisher
from scale.tracker.beads import BeadsTracker, bead_number

pytestmark = pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed")


def _bd(cwd: Path, *args: str) -> str:
    return subprocess.run(["bd", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(cwd),
                               "BD_NON_INTERACTIVE": "1"}).stdout.strip()


def _q(cwd: Path, title: str, *labels: str) -> str:
    bid = _bd(cwd, "q", title)
    for l in labels:
        _bd(cwd, "label", "add", bid, l)
    return bid


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    d = tmp_path_factory.mktemp("beads")
    _bd(d, "init", "--prefix", "t", "--non-interactive", "--skip-agents", "--skip-hooks", "--quiet")
    ids = {
        "ready": _q(d, "Ready agent task", "agent-ok"),
        "unlabeled": _q(d, "Human task"),
        "blocked": _q(d, "Blocked agent task", "agent-ok"),
        "closed": _q(d, "Closed agent task", "agent-ok"),
        "claimed": _q(d, "Claimed agent task", "agent-ok"),
        "skipped": _q(d, "Skipped agent task", "agent-ok", "scale:skip"),
    }
    _bd(d, "dep", "add", ids["blocked"], ids["unlabeled"])       # blocked until the human task closes
    _bd(d, "close", ids["closed"], "--reason", "done")
    _bd(d, "update", ids["claimed"], "--status", "in_progress")
    return d, ids


def _tracker(d: Path) -> BeadsTracker:
    return BeadsTracker(TrackerConfig(kind="beads", beads_path=str(d), repo="o/r", api_token="tok",
                                      active_labels=["agent-ok"]))


@pytest.mark.asyncio
async def test_candidates_are_ready_labeled_unblocked_open(db):
    d, ids = db
    got = {i.id for i in await _tracker(d).fetch_candidate_issues()}
    assert got == {ids["ready"]}


@pytest.mark.asyncio
async def test_issue_shape(db):
    d, ids = db
    [issue] = await _tracker(d).fetch_candidate_issues()
    assert issue.identifier == ids["ready"]
    assert issue.number == bead_number(ids["ready"]) and issue.number > 0
    assert issue.branch_name == f"scale/{ids['ready']}"
    assert issue.title == "Ready agent task" and issue.state == "active"
    assert "agent-ok" in issue.labels


@pytest.mark.asyncio
async def test_fetch_by_numbers_works_on_a_fresh_tracker(db):
    d, ids = db
    n = bead_number(ids["ready"])
    [issue] = await _tracker(d).fetch_issues_by_numbers([n])   # no prior fetch: must resolve itself
    assert issue.id == ids["ready"]


@pytest.mark.asyncio
async def test_states(db):
    d, ids = db
    by_id = {i.id: i for i in await _tracker(d).fetch_issues_by_numbers([bead_number(x) for x in ids.values()])}
    assert by_id[ids["closed"]].state == "terminal"
    assert by_id[ids["claimed"]].state == "ignored"     # a human (or someone) is on it
    assert by_id[ids["skipped"]].state == "ignored"
    assert by_id[ids["unlabeled"]].state == "ignored"


@pytest.mark.asyncio
async def test_labels_comments_and_terminal_close(tmp_path):
    _bd(tmp_path, "init", "--prefix", "u", "--non-interactive", "--skip-agents", "--skip-hooks", "--quiet")
    bid = _q(tmp_path, "Work", "agent-ok")
    t = _tracker(tmp_path)
    n = bead_number(bid)

    await t.add_labels(n, ["scale:pr-open"])
    await t.remove_label(n, "scale:pr-open")
    [i] = await t.fetch_issues_by_numbers([n])
    assert "scale:pr-open" not in i.labels

    await t.post_comment(n, "<!-- scale-attempt-summary -->\nattempt 1 failed: no commits")
    comments = await t.fetch_issue_comments(n)
    assert any("scale-attempt-summary" in c["body"] for c in comments)

    await t.add_labels(n, ["scale:done"])                # terminal label closes the bead
    [i] = await t.fetch_issues_by_numbers([n])
    assert i.state == "terminal"
    assert bid in {x.id for x in await t.fetch_terminal_issues()}
    assert '"status":"closed"' in _bd(tmp_path, "show", bid, "--json").replace(" ", "")


@pytest.mark.asyncio
async def test_create_issue_and_sub_issue(tmp_path):
    _bd(tmp_path, "init", "--prefix", "v", "--non-interactive", "--skip-agents", "--skip-hooks", "--quiet")
    parent = _q(tmp_path, "Parent")
    t = _tracker(tmp_path)
    child = await t.create_issue("Child", "body text", ["agent-ok"])
    assert child["number"] == bead_number(child["node_id"])
    assert await t.add_sub_issue(bead_number(parent), child["node_id"]) is True
    assert parent in _bd(tmp_path, "show", child["node_id"], "--json")


@pytest.mark.asyncio
async def test_publisher_pr_body_has_no_github_closes_for_beads(db):
    from unittest.mock import AsyncMock
    d, ids = db
    cfg = WorkflowConfig(tracker=TrackerConfig(kind="beads", beads_path=str(d), repo="o/r", api_token="tok"),
                         agent=AgentConfig(publish="orchestrator"), prompt_template="x")
    [issue] = await _tracker(d).fetch_candidate_issues()
    scm = AsyncMock(); scm.fetch_pr_for_branch = AsyncMock(return_value=None)
    pub = Publisher(cfg, scm)
    pub.commits_ahead = AsyncMock(return_value=1)
    pub._git = AsyncMock(return_value="")
    await pub.publish(issue, d)
    body = scm.create_pr.await_args.kwargs["body"]
    assert "Closes #" not in body and ids["ready"] in body


def test_make_tracker_factory(db):
    from scale.tracker import make_tracker
    from scale.tracker.github import GitHubClient
    d, _ = db
    assert isinstance(make_tracker(TrackerConfig(kind="beads", beads_path=str(d), repo="o/r", api_token="t")), BeadsTracker)
    assert isinstance(make_tracker(TrackerConfig(repo="o/r", api_token="t")), GitHubClient)
