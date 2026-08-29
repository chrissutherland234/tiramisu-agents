"""Tiramisu API entry point."""

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from tiramisu_agents import __version__
from tiramisu_agents.api.settings import get_settings


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service="tiramisu-api",
        version=__version__,
        environment=settings.environment,
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Tiramisu API",
        summary="Durable, long-running business agents",
        version=__version__,
    )
    app.add_api_route(
        "/health",
        health,
        methods=["GET"],
        response_model=HealthResponse,
        tags=["operations"],
    )

    return app


app = create_app()


def run() -> None:
    uvicorn.run("tiramisu_agents.api.main:app", host="127.0.0.1", port=8000, reload=True)
