"""Tiramisu API entry point."""

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents import __version__
from tiramisu_agents.api.events import fictional_trigger_rules
from tiramisu_agents.api.events import router as events_router
from tiramisu_agents.api.settings import Settings, get_settings
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.events.ingestion import ProcessBootstrap


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


async def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        service="tiramisu-api",
        version=__version__,
        environment=settings.environment,
    )


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    trigger_rules: Mapping[str, ProcessBootstrap] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    owned_engine = None
    if session_factory is None:
        owned_engine = create_engine(resolved_settings.database_url)
        session_factory = create_session_factory(owned_engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        if owned_engine is not None:
            await owned_engine.dispose()

    app = FastAPI(
        title="Tiramisu API",
        summary="Durable, long-running business agents",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.state.trigger_rules = dict(
        trigger_rules
        if trigger_rules is not None
        else fictional_trigger_rules()
        if resolved_settings.load_fictional_example_processes
        else {}
    )
    app.add_api_route(
        "/health",
        health,
        methods=["GET"],
        response_model=HealthResponse,
        tags=["operations"],
    )
    app.include_router(events_router)

    return app


app = create_app()


def run() -> None:
    uvicorn.run("tiramisu_agents.api.main:app", host="127.0.0.1", port=8000, reload=True)
