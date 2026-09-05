"""Tiramisu API entry point."""

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents import __version__
from tiramisu_agents.api.deployment import compose_deployment_release
from tiramisu_agents.api.events import router as events_router
from tiramisu_agents.api.outbox import router as outbox_router
from tiramisu_agents.api.processes import router as processes_router
from tiramisu_agents.api.quarantine import router as quarantine_router
from tiramisu_agents.api.settings import Settings, get_settings
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.events.ingestion import ProcessBootstrap
from tiramisu_agents.extensions import ClientPack, DeploymentRelease, load_configured_client_pack
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str
    deployment_id: str | None = None
    deployment_build_id: str | None = None
    deployment_release_fingerprint: str | None = None
    client_pack_fingerprint: str | None = None
    model_id: str | None = None
    temporal_task_queue: str | None = None


async def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    release: DeploymentRelease | None = request.app.state.deployment_release
    return HealthResponse(
        status="ok",
        service="tiramisu-api",
        version=__version__,
        environment=settings.environment,
        deployment_id=release.deployment_id if release else None,
        deployment_build_id=release.build_id if release else None,
        deployment_release_fingerprint=release.release_fingerprint if release else None,
        client_pack_fingerprint=release.client_pack_fingerprint if release else None,
        model_id=release.model_id if release else None,
        temporal_task_queue=release.temporal_task_queue if release else None,
    )


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    trigger_rules: Mapping[str, ProcessBootstrap] | None = None,
    process_registry: ProcessDefinitionRegistry | None = None,
    client_pack: ClientPack | None = None,
    deployment_release: DeploymentRelease | None = None,
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
    if client_pack is None and (trigger_rules is None or process_registry is None):
        client_pack = load_configured_client_pack(
            resolved_settings.client_pack_factory,
            load_fictional_example=resolved_settings.load_fictional_example_processes,
        )
    if client_pack is not None:
        if deployment_release is None:
            deployment_release = compose_deployment_release(resolved_settings, client_pack)
        else:
            deployment_release.require_client_pack(client_pack.fingerprint())
        if trigger_rules is None:
            trigger_rules = client_pack.trigger_rules(deployment_release)
        else:
            for event_type, bootstrap in trigger_rules.items():
                definition = client_pack.registry.resolve_trigger(event_type)
                if definition is None:
                    raise ValueError("trigger rule is not published by the client pack")
                if (
                    bootstrap.process_type != definition.id
                    or bootstrap.definition_version != definition.version
                ):
                    raise ValueError("trigger rules and client-pack definitions disagree")
                client_pack.compatibility.require_process(
                    process_type=bootstrap.process_type,
                    definition_version=bootstrap.definition_version,
                    client_pack_fingerprint=bootstrap.client_pack_fingerprint,
                    extension_manifest_hash=bootstrap.extension_manifest_hash,
                    process_definition_fingerprint=bootstrap.process_definition_fingerprint,
                )
                deployment_release.require_process(
                    deployment_id=bootstrap.deployment_id,
                    deployment_release_fingerprint=(bootstrap.deployment_release_fingerprint),
                    temporal_task_queue=bootstrap.temporal_task_queue,
                )
        if process_registry is None:
            process_registry = client_pack.registry
    elif deployment_release is not None:
        raise ValueError("a deployment release requires a client pack")
    app.state.client_pack = client_pack
    app.state.deployment_release = deployment_release
    app.state.deployment_tenant_ids = frozenset(resolved_settings.deployment_tenant_ids)
    app.state.trigger_rules = dict(trigger_rules or {})
    app.state.process_registry = process_registry
    app.add_api_route(
        "/health",
        health,
        methods=["GET"],
        response_model=HealthResponse,
        response_model_exclude_none=True,
        tags=["operations"],
    )
    app.include_router(events_router)
    app.include_router(processes_router)
    app.include_router(outbox_router)
    app.include_router(quarantine_router)

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "tiramisu_agents.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment == "development",
        log_level=settings.log_level.lower(),
    )
