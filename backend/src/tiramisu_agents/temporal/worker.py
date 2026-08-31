"""Temporal worker and tenant-scoped outbox dispatcher entry point."""

import argparse
import asyncio
import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from temporalio.client import Client
from temporalio.worker import Worker

from tiramisu_agents.actions.execution import ActionExecutor
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.agents.openai_runner import OpenAIAgentsTurnRunner
from tiramisu_agents.api.settings import Settings, get_settings
from tiramisu_agents.builtin import FictionalDeployment, load_fictional_deployment
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.extensions import ClientPack, load_configured_client_pack
from tiramisu_agents.temporal.activities.action_execution import ActionExecutionActivities
from tiramisu_agents.temporal.activities.action_gateway import ActionGatewayActivities
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities
from tiramisu_agents.temporal.activities.process_state import ProcessStateActivities
from tiramisu_agents.temporal.dispatcher import TemporalOutboxDispatcher
from tiramisu_agents.temporal.workflows.mailbox import ProcessMailboxWorkflow

_CLIENT_PACK_UNSET = object()


def resolve_worker_tenants(
    settings: Settings, cli_tenant_ids: tuple[UUID, ...] = ()
) -> tuple[UUID, ...]:
    """CLI assignments replace environment assignments to avoid accidental scope expansion."""
    tenant_ids = cli_tenant_ids or settings.worker_tenant_ids
    if not tenant_ids:
        raise ValueError("at least one deployment-authorized worker tenant ID is required")
    if len(tenant_ids) != len(set(tenant_ids)):
        raise ValueError("worker tenant assignments must be unique")
    return tenant_ids


def compose_fictional_worker(settings: Settings) -> FictionalDeployment:
    """Compatibility helper for the bundled local example."""
    _require_agent_model_configuration(settings)
    return load_fictional_deployment()


def compose_worker_client_pack(settings: Settings) -> ClientPack | None:
    deployment = load_configured_client_pack(
        settings.client_pack_factory,
        load_fictional_example=settings.load_fictional_example_processes,
    )
    if deployment is not None:
        _require_agent_model_configuration(settings)
    return deployment


def _require_agent_model_configuration(settings: Settings) -> None:
    if settings.openai_model is None:
        raise ValueError(
            "TIRAMISU_OPENAI_MODEL is required when client-pack orchestration is enabled"
        )
    if settings.openai_api_key is None:
        raise ValueError("OPENAI_API_KEY is required when client-pack orchestration is enabled")


async def serve(
    tenant_ids: tuple[UUID, ...],
    *,
    settings: Settings | None = None,
    client_pack: ClientPack | None | object = _CLIENT_PACK_UNSET,
) -> None:
    settings = settings or get_settings()
    tenant_ids = resolve_worker_tenants(settings, tenant_ids)
    deployment = (
        compose_worker_client_pack(settings) if client_pack is _CLIENT_PACK_UNSET else client_pack
    )
    if deployment is not None and not isinstance(deployment, ClientPack):
        raise TypeError("client_pack must be a ClientPack or None")
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    client = await Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
    )
    activities: list[Callable[..., Any]] = []
    authorized_tenant_ids = frozenset(tenant_ids)
    orchestrate_agent_turns = False
    if deployment is not None:
        assert settings.openai_model is not None
        assert settings.openai_api_key is not None
        registry = deployment.registry
        agent_activities = AgentTurnActivities(
            session_factory,
            registry,
            OpenAIAgentsTurnRunner(
                model=settings.openai_model,
                api_key=settings.openai_api_key.get_secret_value(),
                output_type=deployment.agent_decision_output_type,
            ),
            authorized_tenant_ids=authorized_tenant_ids,
        )
        gateway_activities = ActionGatewayActivities(
            session_factory,
            registry,
            authorized_tenant_ids=authorized_tenant_ids,
        )
        state_activities = ProcessStateActivities(
            session_factory,
            registry,
            authorized_tenant_ids=authorized_tenant_ids,
        )
        execution_activities = ActionExecutionActivities(
            ActionExecutor(
                session_factory,
                ActionAdapterRegistry(deployment.bindings),
            ),
            authorized_tenant_ids=authorized_tenant_ids,
        )
        activities = [
            agent_activities.run_agent_turn,
            gateway_activities.persist_agent_actions,
            state_activities.persist_process_state,
            state_activities.record_process_intervention,
            execution_activities.execute_action,
            execution_activities.reconcile_action,
        ]
        orchestrate_agent_turns = True
    dispatcher = TemporalOutboxDispatcher(
        session_factory,
        client,
        task_queue=settings.temporal_task_queue,
        orchestrate_agent_turns=orchestrate_agent_turns,
    )
    try:
        async with (
            Worker(
                client,
                task_queue=settings.temporal_task_queue,
                workflows=[ProcessMailboxWorkflow],
                activities=activities,
            ),
            asyncio.TaskGroup() as tasks,
        ):
            for tenant_id in tenant_ids:
                tasks.create_task(
                    dispatcher.run_tenant(tenant_id),
                    name=f"outbox-{tenant_id}",
                )
    finally:
        await engine.dispose()


def run() -> None:
    parser = argparse.ArgumentParser(description="Run the Tiramisu Temporal worker")
    parser.add_argument(
        "--tenant-id",
        action="append",
        type=UUID,
        default=[],
        help=(
            "deployment-authorized tenant UUID; repeat for multiple tenants; "
            "when supplied, replaces TIRAMISU_WORKER_TENANT_IDS"
        ),
    )
    arguments = parser.parse_args()
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    try:
        tenant_ids = resolve_worker_tenants(settings, tuple(arguments.tenant_id))
        client_pack = compose_worker_client_pack(settings)
    except ValueError as error:
        parser.error(str(error))
    asyncio.run(serve(tenant_ids, settings=settings, client_pack=client_pack))
