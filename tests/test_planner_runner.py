import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from symphony.planner.runner import PlannerRunner, _parse_plan_marker, _build_marker, _get_depth
from symphony.planner.agent import PlanAssessment, ChildSpec
from symphony.tracker.models import Issue
from symphony.config.schema import CodexConfig, PlannerConfig


def _config(**kwargs) -> PlannerConfig:
    return PlannerConfig(**kwargs)


def _codex() -> CodexConfig:
    return CodexConfig()


def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="o/r#1", number=1, title="Big feature",
        description="A high-level concept.",
        state="active", labels=[], branch_name="symphony/1-big-feature",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 2),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def test_parse_plan_marker_valid():
    body = '<!-- symphony-plan {"children": [51, 52], "depth": 0} -->'
    data = _parse_plan_marker(body)
    assert data == {"children": [51, 52], "depth": 0}


def test_parse_plan_marker_not_a_marker():
    assert _parse_plan_marker("not a marker") is None


def test_build_marker_roundtrip():
    marker = _build_marker([51, 52, 53], 1)
    data = _parse_plan_marker(marker)
    assert data["children"] == [51, 52, 53]
    assert data["depth"] == 1


def test_get_depth_no_label():
    issue = _issue(labels=[])
    assert _get_depth(issue) == 0


def test_get_depth_with_label():
    issue = _issue(labels=["symphony:depth:2", "symphony:ready"])
    assert _get_depth(issue) == 2


@pytest.mark.asyncio
async def test_plan_issue_already_planned_skips(tmp_path):
    issue = _issue(labels=["symphony:planned"])
    gh = AsyncMock()
    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    await runner.plan_issue(issue, force=False)
    gh.fetch_issue_comments.assert_not_called()


@pytest.mark.asyncio
async def test_plan_issue_already_planned_force_proceeds(tmp_path):
    issue = _issue(labels=["symphony:planned"])
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []
    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    assessment = PlanAssessment(is_leaf=True)
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.plan_issue(issue, force=True)
    gh.add_labels.assert_called_once()


@pytest.mark.asyncio
async def test_plan_issue_leaf_applies_leaf_label(tmp_path):
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []
    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    with patch.object(runner._agent, "assess", AsyncMock(return_value=PlanAssessment(is_leaf=True))):
        await runner.plan_issue(issue)
    gh.add_labels.assert_called_once_with(1, ["symphony:leaf"])
    gh.remove_label.assert_called_once_with(1, "symphony:plan")


@pytest.mark.asyncio
async def test_plan_issue_concept_creates_children_and_labels(tmp_path):
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []
    gh.create_issue.side_effect = [
        {"number": 51, "node_id": "node51"},
        {"number": 52, "node_id": "node52"},
    ]
    gh.add_sub_issue.return_value = True

    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    assessment = PlanAssessment(is_leaf=False, children=[
        ChildSpec(title="Child A", description="Do A", labels=["symphony:ready"]),
        ChildSpec(title="Child B", description="Do B", labels=["symphony:ready"]),
    ])
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.plan_issue(issue)

    assert gh.create_issue.call_count == 2
    assert gh.post_comment.call_count == 1
    comment_body = gh.post_comment.call_args[0][1]
    assert "51" in comment_body
    assert "52" in comment_body

    final_labels = gh.add_labels.call_args_list[-1][0][1]
    assert "symphony:concept" in final_labels
    assert "symphony:planned" in final_labels
    gh.remove_label.assert_called_with(1, "symphony:plan")


@pytest.mark.asyncio
async def test_plan_issue_dry_run_no_github_writes(tmp_path, capsys):
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []
    runner = PlannerRunner(_config(), _codex(), gh, dry_run=True)
    runner._workspace = tmp_path
    assessment = PlanAssessment(is_leaf=False, children=[
        ChildSpec(title="Child A", description="Do A", labels=[]),
    ])
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.plan_issue(issue)

    gh.create_issue.assert_not_called()
    gh.post_comment.assert_not_called()
    captured = capsys.readouterr()
    assert "Child A" in captured.out


@pytest.mark.asyncio
async def test_get_child_numbers_from_marker(tmp_path):
    issue = _issue()
    gh = AsyncMock()
    marker = _build_marker([51, 52, 53], 0)
    gh.fetch_issue_comments.return_value = [
        {"body": "some other comment"},
        {"body": marker},
    ]
    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    numbers = await runner.get_child_numbers(issue)
    assert numbers == [51, 52, 53]
