from __future__ import annotations
import asyncio
import textwrap
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from scale.config.schema import (
    WorkflowConfig, TrackerConfig, ReviewConfig,
)
from scale.tracker.models import Issue


def _issue(number=1, labels=None) -> Issue:
    return Issue(
        id="i1", identifier=f"o/r#{number}", number=number,
        title="Fix bug", description="A bug to fix", state="active",
        labels=labels or [],
        branch_name=f"symphony/{number}-fix-bug",
        url=f"https://github.com/o/r/issues/{number}",
        priority=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _config_with_review(**review_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
        review=ReviewConfig(**review_kwargs),
    )


def _config_no_review() -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_review_config_defaults():
    cfg = ReviewConfig()
    assert cfg.model == "claude-haiku-4-5-20251001"
    assert cfg.timeout_ms == 120000
    assert cfg.pr_open_label == "symphony:pr-open"
    assert cfg.needs_revision_label == "symphony:needs-revision"
    assert cfg.conflict_label == "symphony:conflict"
    assert cfg.template == ""


def test_workflow_config_review_defaults_none():
    cfg = WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        prompt_template="t",
    )
    assert cfg.review is None


def test_workflow_config_with_review():
    cfg = WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        prompt_template="t",
        review=ReviewConfig(model="claude-opus-4-7", timeout_ms=60000),
    )
    assert cfg.review is not None
    assert cfg.review.model == "claude-opus-4-7"
    assert cfg.review.timeout_ms == 60000
    assert cfg.review.pr_open_label == "symphony:pr-open"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_load_workflow_loads_review_md(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok123")
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(textwrap.dedent("""\
        ---
        tracker:
          kind: github
          repo: o/r
          api_token: $GH_TOKEN
        ---
        Work on {{ issue.title }}.
    """))
    rv = tmp_path / "REVIEW.md"
    rv.write_text(textwrap.dedent("""\
        ---
        review:
          model: claude-haiku-4-5-20251001
          timeout_ms: 90000
        ---
        Review PR #{{ pr.number }} for issue #{{ issue.number }}.
    """))

    from scale.config.loader import load_workflow
    cfg = load_workflow(wf)

    assert cfg.review is not None
    assert cfg.review.model == "claude-haiku-4-5-20251001"
    assert cfg.review.timeout_ms == 90000
    assert "Review PR" in cfg.review.template
    assert "{{ pr.number }}" in cfg.review.template


def test_load_workflow_without_review_md(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(textwrap.dedent("""\
        ---
        tracker:
          kind: github
          repo: o/r
          api_token: $GH_TOKEN
        ---
        workflow prompt
    """))

    from scale.config.loader import load_workflow
    cfg = load_workflow(wf)

    assert cfg.review is None


def test_load_workflow_review_md_default_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(textwrap.dedent("""\
        ---
        tracker:
          kind: github
          repo: o/r
          api_token: $GH_TOKEN
        ---
        prompt
    """))
    rv = tmp_path / "REVIEW.md"
    rv.write_text(textwrap.dedent("""\
        ---
        review: {}
        ---
        review template
    """))

    from scale.config.loader import load_workflow
    cfg = load_workflow(wf)

    assert cfg.review is not None
    assert cfg.review.model == "claude-haiku-4-5-20251001"
    assert cfg.review.pr_open_label == "symphony:pr-open"


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def test_render_review_prompt_includes_issue_and_pr():
    from scale.prompt.renderer import render_review_prompt
    template = "Issue #{{ issue.number }}: {{ issue.title }}\nPR #{{ pr.number }}: {{ pr.url }}\n{{ pr.diff }}"
    issue = _issue(number=7)
    result = render_review_prompt(
        template, issue,
        pr_number=42, pr_url="https://github.com/o/r/pull/42", pr_diff="diff --git a/f b/f",
    )
    assert "Issue #7: Fix bug" in result
    assert "PR #42: https://github.com/o/r/pull/42" in result
    assert "diff --git a/f b/f" in result


def test_render_review_prompt_includes_safety_preamble():
    from scale.prompt.renderer import render_review_prompt
    template = "{{ issue.title }}"
    issue = _issue()
    result = render_review_prompt(template, issue, pr_number=1, pr_url="u", pr_diff="d")
    assert "autonomous coding agent" in result


def test_render_review_prompt_issue_description():
    from scale.prompt.renderer import render_review_prompt
    template = "{{ issue.description }} {{ issue.url }}"
    issue = _issue()
    result = render_review_prompt(template, issue, pr_number=1, pr_url="u", pr_diff="d")
    assert "A bug to fix" in result
    assert issue.url in result


# ---------------------------------------------------------------------------
# GitHub client – PR methods
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_pr_for_branch_returns_pr():
    import httpx
    from scale.tracker.github import GitHubClient
    from scale.config.schema import TrackerConfig

    client = GitHubClient(TrackerConfig(repo="owner/repo", api_token="tok"))

    pr_data = [{"number": 5, "html_url": "https://github.com/o/r/pull/5"}]
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        resp = MagicMock()
        resp.json.return_value = pr_data
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        result = await client.fetch_pr_for_branch("symphony/1-fix-bug")

    assert result is not None
    assert result["number"] == 5


@pytest.mark.asyncio
async def test_fetch_pr_for_branch_returns_none_when_no_prs():
    from scale.tracker.github import GitHubClient
    from scale.config.schema import TrackerConfig

    client = GitHubClient(TrackerConfig(repo="owner/repo", api_token="tok"))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        resp = MagicMock()
        resp.json.return_value = []
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        result = await client.fetch_pr_for_branch("symphony/1-fix-bug")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_pr_diff_returns_text():
    from scale.tracker.github import GitHubClient
    from scale.config.schema import TrackerConfig

    client = GitHubClient(TrackerConfig(repo="owner/repo", api_token="tok"))
    diff_text = "diff --git a/file.py b/file.py\n+++ added line"

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        resp = MagicMock()
        resp.text = diff_text
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        result = await client.fetch_pr_diff(5)

    assert result == diff_text


# ---------------------------------------------------------------------------
# Orchestrator – review dispatch
# ---------------------------------------------------------------------------

from scale.orchestrator.core import Orchestrator
from scale.orchestrator.state import LiveSession


@pytest.mark.asyncio
async def test_run_worker_adds_pr_open_label_when_review_configured():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    config = _config_with_review()
    orch = Orchestrator(config, tracker)

    added_labels: list[tuple] = []

    async def _mock_run(iss, cfg, attempt, on_event=None):
        pass

    async def _mock_add_labels(number, labels):
        added_labels.append((number, labels))

    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", side_effect=_mock_add_labels), \
         patch.object(orch._workspace, "remove", AsyncMock()):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    label_calls = [labels for _, labels in added_labels]
    assert any("symphony:pr-open" in labels for labels in label_calls)
    assert not any("scale:done" in labels for labels in label_calls)


@pytest.mark.asyncio
async def test_run_worker_adds_terminal_label_when_no_review():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    config = _config_no_review()
    orch = Orchestrator(config, tracker)

    added_labels: list[tuple] = []

    async def _mock_run(iss, cfg, attempt, on_event=None):
        pass

    async def _mock_add_labels(number, labels):
        added_labels.append((number, labels))

    issue = _issue()
    task = asyncio.create_task(asyncio.sleep(0))
    orch._state.running[issue.id] = LiveSession(issue=issue, task=task)
    orch._state.claimed.add(issue.id)

    with patch("scale.orchestrator.core.LocalWorker") as MockWorker, \
         patch.object(orch._github, "add_labels", side_effect=_mock_add_labels), \
         patch.object(orch._workspace, "remove", AsyncMock()):
        mock_w = MagicMock()
        mock_w.run = _mock_run
        MockWorker.return_value = mock_w
        await orch._run_worker(issue, attempt=None)

    label_calls = [labels for _, labels in added_labels]
    assert any("scale:done" in labels for labels in label_calls)


@pytest.mark.asyncio
async def test_tick_dispatches_pr_open_issues_to_reviewer():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    pr_open_issue = _issue(number=5, labels=["symphony:pr-open"])

    config = _config_with_review()
    orch = Orchestrator(config, tracker)
    orch._github = AsyncMock()
    orch._github.fetch_issues_by_label.return_value = [pr_open_issue]
    orch._github.fetch_candidate_issues = AsyncMock(return_value=[])

    with patch.object(orch, "_run_reviewer", AsyncMock()) as mock_reviewer:
        await orch._tick()
        await asyncio.sleep(0)

    mock_reviewer.assert_called_once()
    assert mock_reviewer.call_args[0][0].number == 5


@pytest.mark.asyncio
async def test_tick_skips_review_dispatch_when_not_configured():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    config = _config_no_review()
    orch = Orchestrator(config, tracker)
    orch._github = AsyncMock()
    orch._github.fetch_candidate_issues = AsyncMock(return_value=[])

    await orch._tick()

    orch._github.fetch_issues_by_label.assert_not_called()


@pytest.mark.asyncio
async def test_tick_does_not_redispatch_claimed_pr_open_issues():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    pr_open_issue = _issue(number=5, labels=["symphony:pr-open"])

    config = _config_with_review()
    orch = Orchestrator(config, tracker)
    orch._state.claimed.add(pr_open_issue.id)
    orch._github = AsyncMock()
    orch._github.fetch_issues_by_label.return_value = [pr_open_issue]
    orch._github.fetch_candidate_issues = AsyncMock(return_value=[])

    with patch.object(orch, "_run_reviewer", AsyncMock()) as mock_reviewer:
        await orch._tick()
        await asyncio.sleep(0)

    mock_reviewer.assert_not_called()


@pytest.mark.asyncio
async def test_run_reviewer_success_adds_terminal_and_removes_pr_open():
    tracker = AsyncMock()
    config = _config_with_review()
    orch = Orchestrator(config, tracker)

    issue = _issue(number=3, labels=["symphony:pr-open"])

    pr_data = {"number": 10, "html_url": "https://github.com/o/r/pull/10"}

    add_calls: list[tuple] = []
    remove_calls: list[tuple] = []

    async def _mock_add(number, labels):
        add_calls.append((number, labels))

    async def _mock_remove(number, label):
        remove_calls.append((number, label))

    with patch.object(orch._github, "fetch_pr_for_branch", AsyncMock(return_value=pr_data)), \
         patch.object(orch._github, "fetch_pr_diff", AsyncMock(return_value="diff text")), \
         patch.object(orch._github, "add_labels", side_effect=_mock_add), \
         patch.object(orch._github, "remove_label", side_effect=_mock_remove), \
         patch("scale.orchestrator.core.ReviewWorker") as MockReviewer:
        mock_rw = MagicMock()
        mock_rw.run = AsyncMock()
        MockReviewer.return_value = mock_rw
        orch._state.claimed.add(issue.id)
        await orch._run_reviewer(issue)

    label_calls = [labels for _, labels in add_calls]
    assert any("scale:done" in labels for labels in label_calls)
    assert any((n, lbl) == (issue.number, "symphony:pr-open") for n, lbl in remove_calls)
    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_run_reviewer_failure_adds_conflict_label():
    tracker = AsyncMock()
    config = _config_with_review()
    orch = Orchestrator(config, tracker)

    issue = _issue(number=3, labels=["symphony:pr-open"])
    pr_data = {"number": 10, "html_url": "https://github.com/o/r/pull/10"}

    add_calls: list[tuple] = []
    remove_calls: list[tuple] = []

    async def _mock_add(number, labels):
        add_calls.append((number, labels))

    async def _mock_remove(number, label):
        remove_calls.append((number, label))

    with patch.object(orch._github, "fetch_pr_for_branch", AsyncMock(return_value=pr_data)), \
         patch.object(orch._github, "fetch_pr_diff", AsyncMock(return_value="diff text")), \
         patch.object(orch._github, "add_labels", side_effect=_mock_add), \
         patch.object(orch._github, "remove_label", side_effect=_mock_remove), \
         patch("scale.orchestrator.core.ReviewWorker") as MockReviewer:
        mock_rw = MagicMock()
        mock_rw.run = AsyncMock(side_effect=RuntimeError("merge conflict"))
        MockReviewer.return_value = mock_rw
        orch._state.claimed.add(issue.id)
        await orch._run_reviewer(issue)

    label_calls = [labels for _, labels in add_calls]
    assert any("symphony:conflict" in labels for labels in label_calls)
    assert not any("scale:done" in labels for labels in label_calls)
    assert any((n, lbl) == (issue.number, "symphony:pr-open") for n, lbl in remove_calls)
    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_run_reviewer_no_pr_skips_review():
    tracker = AsyncMock()
    config = _config_with_review()
    orch = Orchestrator(config, tracker)

    issue = _issue(number=3, labels=["symphony:pr-open"])

    add_calls: list[tuple] = []

    with patch.object(orch._github, "fetch_pr_for_branch", AsyncMock(return_value=None)), \
         patch.object(orch._github, "add_labels", side_effect=lambda n, l: add_calls.append((n, l))):
        orch._state.claimed.add(issue.id)
        await orch._run_reviewer(issue)

    assert not any("scale:done" in labels for _, labels in add_calls)
    assert not any("symphony:conflict" in labels for _, labels in add_calls)
    assert issue.id not in orch._state.claimed


@pytest.mark.asyncio
async def test_tick_review_fetch_failure_does_not_crash():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    config = _config_with_review()
    orch = Orchestrator(config, tracker)
    orch._github = AsyncMock()
    orch._github.fetch_candidate_issues = AsyncMock(return_value=[])
    orch._github.fetch_issues_by_label = AsyncMock(side_effect=RuntimeError("network down"))

    await orch._tick()  # must not raise


# ---------------------------------------------------------------------------
# ReviewWorker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_worker_runs_in_temp_dir():
    from scale.worker.review import ReviewWorker

    config = _config_with_review(template="Review {{ issue.title }} PR #{{ pr.number }}")
    worker = ReviewWorker(config)

    issue = _issue()

    captured_workspaces: list = []

    async def _mock_run_turn(workspace, prompt, is_continuation, on_event=None, model=None):
        captured_workspaces.append(workspace)
        from scale.agent.claude import TurnResult
        return TurnResult(success=True, usage=None, message="done")

    with patch.object(worker._runner, "run_turn", side_effect=_mock_run_turn):
        await worker.run(issue, pr_number=5, pr_url="https://u", pr_diff="diff")

    assert len(captured_workspaces) == 1
    import os
    assert not os.path.exists(str(captured_workspaces[0]))


@pytest.mark.asyncio
async def test_review_worker_passes_correct_model():
    from scale.worker.review import ReviewWorker

    config = _config_with_review(model="claude-haiku-4-5-20251001", template="{{ issue.title }}")
    worker = ReviewWorker(config)
    issue = _issue()

    captured_models: list = []

    async def _mock_run_turn(workspace, prompt, is_continuation, on_event=None, model=None):
        captured_models.append(model)
        from scale.agent.claude import TurnResult
        return TurnResult(success=True, usage=None, message="done")

    with patch.object(worker._runner, "run_turn", side_effect=_mock_run_turn):
        await worker.run(issue, pr_number=5, pr_url="https://u", pr_diff="diff")

    assert captured_models == ["claude-haiku-4-5-20251001"]


@pytest.mark.asyncio
async def test_review_worker_raises_on_failure():
    from scale.worker.review import ReviewWorker

    config = _config_with_review(template="{{ issue.title }}")
    worker = ReviewWorker(config)
    issue = _issue()

    async def _mock_run_turn(workspace, prompt, is_continuation, on_event=None, model=None):
        from scale.agent.claude import TurnResult
        return TurnResult(success=False, usage=None, message="agent error")

    with patch.object(worker._runner, "run_turn", side_effect=_mock_run_turn):
        with pytest.raises(RuntimeError, match="Review failed"):
            await worker.run(issue, pr_number=5, pr_url="https://u", pr_diff="diff")


@pytest.mark.asyncio
async def test_review_worker_prompt_contains_pr_context():
    from scale.worker.review import ReviewWorker

    config = _config_with_review(
        template="Issue #{{ issue.number }} PR #{{ pr.number }} diff: {{ pr.diff }}"
    )
    worker = ReviewWorker(config)
    issue = _issue(number=7)

    captured_prompts: list = []

    async def _mock_run_turn(workspace, prompt, is_continuation, on_event=None, model=None):
        captured_prompts.append(prompt)
        from scale.agent.claude import TurnResult
        return TurnResult(success=True, usage=None, message="done")

    with patch.object(worker._runner, "run_turn", side_effect=_mock_run_turn):
        await worker.run(issue, pr_number=42, pr_url="https://u", pr_diff="the diff content")

    assert len(captured_prompts) == 1
    assert "Issue #7" in captured_prompts[0]
    assert "PR #42" in captured_prompts[0]
    assert "the diff content" in captured_prompts[0]
