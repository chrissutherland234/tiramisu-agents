"""Temporal worker and tenant-scoped outbox dispatcher entry point."""

import argparse
import asyncio
from uuid import UUID

from temporalio.client import Client
from temporalio.worker import Worker

from tiramisu_agents.api.settings import get_settings
from tiramisu_agents.db.session import create_engine, create_session_factory
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
    dispatcher = TemporalOutboxDispatcher(
        session_factory,
        client,
        task_queue=settings.temporal_task_queue,
    )
    try:
        async with (
            Worker(
                client,
                task_queue=settings.temporal_task_queue,
                workflows=[ProcessMailboxWorkflow],
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
