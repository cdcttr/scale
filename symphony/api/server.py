from __future__ import annotations
from fastapi import FastAPI
from symphony.api.routes import build_router


def create_app(orchestrator) -> FastAPI:
    app = FastAPI(title="Symphony", version="0.1.0")
    app.include_router(build_router(orchestrator), prefix="/api/v1")
    return app
