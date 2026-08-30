"""Temporal worker and tenant-scoped outbox dispatcher entry point."""

import argparse
import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from temporalio.client import Client
from temporalio.worker import Worker

from tiramisu_agents.actions.execution import ActionExecutor
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.agents.openai_runner import OpenAIAgentsTurnRunner
from tiramisu_agents.api.settings import get_settings
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.temporal.activities.action_execution import ActionExecutionActivities
from tiramisu_agents.temporal.activities.action_gateway import ActionGatewayActivities
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities
from tiramisu_agents.temporal.dispatcher import TemporalOutboxDispatcher
from tiramisu_agents.temporal.workflows.mailbox import ProcessMailboxWorkflow


async def serve(tenant_ids: tuple[UUID, ...]) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    client = await Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
    )
    activities: list[Callable[..., Any]] = []
    orchestrate_agent_turns = False
    if settings.load_fictional_example_processes:
        if settings.openai_model is None:
            raise RuntimeError(
                "TIRAMISU_OPENAI_MODEL is required when fictional process orchestration is enabled"
            )
        registry = ProcessDefinitionRegistry.from_yaml_files(
            [Path("process_definitions/examples/enquiry_to_booking.v1.yaml")]
        )
        agent_activities = AgentTurnActivities(
            session_factory,
            registry,
            OpenAIAgentsTurnRunner(model=settings.openai_model),
        )
        gateway_activities = ActionGatewayActivities(session_factory, registry)
        stub_adapter = StubActionAdapter()
        execution_activities = ActionExecutionActivities(
            ActionExecutor(
                session_factory,
                ActionAdapterRegistry(
                    dict.fromkeys(
                        registry.get("enquiry_to_booking", "1").allowed_actions,
                        stub_adapter,
                    )
                ),
            )
        )
        activities = [
            agent_activities.run_agent_turn,
            gateway_activities.persist_agent_actions,
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
        required=True,
        type=UUID,
        help="deployment-authorized tenant UUID; repeat for multiple tenants",
    )
    arguments = parser.parse_args()
    asyncio.run(serve(tuple(arguments.tenant_id)))
