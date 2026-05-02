from __future__ import annotations
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from symphony.orchestrator.state import LiveSession, OrchestratorState, RetryEntry

_bearer = HTTPBearer(auto_error=False)


def _make_auth_dep(api_token: Optional[str]):
    def verify(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> None:
        if api_token is None:
            return
        if credentials is None or credentials.credentials != api_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return verify


def _serialize_session(session: LiveSession) -> dict[str, Any]:
    return {
        "issue_identifier": session.issue.identifier,
        "title": session.issue.title,
        "turn_count": session.turn_count,
        "tokens": {
            "input": session.tokens.input_tokens,
            "output": session.tokens.output_tokens,
        },
        "started_at": session.started_at.isoformat(),
        "last_event_at": session.last_event_at.isoformat(),
    }


def _serialize_retry(entry: RetryEntry) -> dict[str, Any]:
    return {
        "issue_identifier": entry.issue.identifier,
        "attempt": entry.attempt,
        "due_at": entry.due_at.isoformat(),
        "error": entry.error,
    }


def build_router(orchestrator, api_token: Optional[str] = None) -> APIRouter:
    router = APIRouter(dependencies=[Depends(_make_auth_dep(api_token))])

    @router.get("/state")
    def get_state() -> dict[str, Any]:
        state: OrchestratorState = orchestrator.get_state()
        return {
            "running": [_serialize_session(s) for s in state.running.values()],
            "retrying": [_serialize_retry(e) for e in state.retry_queue],
            "token_totals": {
                "input": state.token_totals.input_tokens,
                "output": state.token_totals.output_tokens,
                "total": state.token_totals.total,
            },
            "agent_count": {
                "running": len(state.running),
                "retrying": len(state.retry_queue),
                "completed": len(state.completed),
            },
        }

    @router.get("/{issue_identifier}")
    def get_issue(issue_identifier: str) -> dict[str, Any]:
        state: OrchestratorState = orchestrator.get_state()
        for session in state.running.values():
            normalized = (
                session.issue.identifier.replace("/", "-").replace("#", "-")
            )
            if normalized == issue_identifier:
                return _serialize_session(session)
        raise HTTPException(status_code=404, detail="Issue not found")

    @router.post("/refresh")
    def refresh() -> dict[str, str]:
        orchestrator.request_refresh()
        return {"status": "queued"}

    return router
