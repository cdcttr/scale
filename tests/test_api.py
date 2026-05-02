import asyncio
import pytest
from datetime import datetime
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from symphony.api.server import create_app
from symphony.orchestrator.state import OrchestratorState, LiveSession, TokenTotals
from symphony.tracker.models import Issue

_TOKEN = "test-secret-token"


def _issue(number=42) -> Issue:
    return Issue(
        id=f"n{number}", identifier=f"o/r#{number}", number=number,
        title="Fix bug", description="", state="active",
        labels=[], branch_name=f"symphony/{number}-fix-bug",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


def _orch_with_state(state: OrchestratorState):
    orch = MagicMock()
    orch.get_state.return_value = state
    orch.request_refresh = MagicMock()
    return orch


def _make_task():
    loop = asyncio.new_event_loop()
    task = loop.create_task(asyncio.sleep(0))
    return task


def test_state_endpoint_empty():
    orch = _orch_with_state(OrchestratorState())
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.get("/api/v1/state", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 200
    data = r.json()
    assert data["running"] == []
    assert data["retrying"] == []
    assert data["agent_count"]["running"] == 0


def test_state_endpoint_with_running_session():
    state = OrchestratorState()
    session = LiveSession(issue=_issue(), task=MagicMock())
    state.running["n42"] = session
    orch = _orch_with_state(state)
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.get("/api/v1/state", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 200
    running = r.json()["running"]
    assert len(running) == 1
    assert running[0]["issue_identifier"] == "o/r#42"


def test_refresh_endpoint():
    orch = _orch_with_state(OrchestratorState())
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.post("/api/v1/refresh", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 200
    orch.request_refresh.assert_called_once()


def test_issue_detail_endpoint():
    state = OrchestratorState()
    session = LiveSession(issue=_issue(42), task=MagicMock())
    state.running["n42"] = session
    orch = _orch_with_state(state)
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.get("/api/v1/o-r-42", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 200
    assert r.json()["issue_identifier"] == "o/r#42"


def test_issue_detail_not_found():
    orch = _orch_with_state(OrchestratorState())
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.get("/api/v1/nonexistent-99", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 404


def test_state_requires_auth():
    orch = _orch_with_state(OrchestratorState())
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.get("/api/v1/state")
    assert r.status_code == 401


def test_refresh_requires_auth():
    orch = _orch_with_state(OrchestratorState())
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.post("/api/v1/refresh")
    assert r.status_code == 401


def test_issue_detail_requires_auth():
    state = OrchestratorState()
    session = LiveSession(issue=_issue(42), task=MagicMock())
    state.running["n42"] = session
    orch = _orch_with_state(state)
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.get("/api/v1/o-r-42")
    assert r.status_code == 401


def test_wrong_token_rejected():
    orch = _orch_with_state(OrchestratorState())
    app = create_app(orch, api_token=_TOKEN)
    with TestClient(app) as client:
        r = client.get("/api/v1/state", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
