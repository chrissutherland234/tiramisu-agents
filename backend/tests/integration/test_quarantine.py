"""Quarantine resolution across PostgreSQL, authenticated API, and Temporal delivery."""

import asyncio
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker
from tiramisu_agents.api.main import create_app
from tiramisu_agents.api.settings import Settings
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.events import (
    EventInbox,
    EventResolutionCommand,
    ExternalCorrelation,
    OutboxMessage,
)
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.events.quarantine import (
    QuarantineConflict,
    QuarantineNotFound,
    QuarantineResolutionService,
    ResolveQuarantineInput,
)
from tiramisu_agents.security.credential_service import TenantCredentialService
from tiramisu_agents.security.credentials import CredentialScope
from tiramisu_agents.security.tenancy import TenantNotAuthorized, TenantSuspended
from tiramisu_agents.temporal.dispatcher import DispatchStatus, TemporalOutboxDispatcher
from tiramisu_agents.temporal.workflows.mailbox import (
    MailboxInput,
    ProcessMailboxWorkflow,
    WakePlan,
)
from tiramisu_agents.testkit.deployment import TEST_DEPLOYMENT_RELEASE
from tiramisu_agents.testkit.temporal_environment import start_time_skipping_environment

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1", reason="requires PostgreSQL"
)


@dataclass
class Context:
    runtime: async_sessionmaker[AsyncSession]
    admin: async_sessionmaker[AsyncSession]
    tenant_id: UUID
    other_tenant_id: UUID
    process_id: UUID
    second_process_id: UUID
    other_process_id: UUID
    actor_id: UUID

    async def ingest(self, event: CanonicalEvent) -> None:
        async with self.runtime.begin() as session:
            await EventIngestionService().ingest(session, event)

    def command(self, event: CanonicalEvent, **changes: Any) -> ResolveQuarantineInput:
        return replace(
            ResolveQuarantineInput(
                command_id=uuid4(),
                tenant_id=self.tenant_id,
                event_id=event.event_id,
                process_instance_id=self.process_id,
                actor_id=self.actor_id,
                reason="Verified the customer thread against the case record",
            ),
            **changes,
        )

    async def resolve(self, command: ResolveQuarantineInput) -> EventResolutionCommand:
        async with self.runtime.begin() as session:
            return await QuarantineResolutionService().resolve(session, command)


def event_for(context: Context, **changes: Any) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=context.tenant_id,
        event_type="customer.email_received",
        source="quarantine.test",
        source_event_id=str(uuid4()),
        occurred_at=datetime.now(UTC),
        payload={"body": "Original customer message"},
        **changes,
    )


def reference(value: str) -> ExternalReference:
    return ExternalReference(provider="quarantine.test", resource_type="thread", external_id=value)


def bootstrap() -> ProcessBootstrap:
    release = TEST_DEPLOYMENT_RELEASE
    return ProcessBootstrap(
        process_type="enquiry_to_booking",
        definition_version="1",
        extension_manifest_hash="a" * 64,
        client_pack_fingerprint="b" * 64,
        process_definition_fingerprint="c" * 64,
        deployment_id=release.deployment_id,
        deployment_release_fingerprint=release.release_fingerprint,
        temporal_task_queue=release.temporal_task_queue,
    )


@pytest.fixture
async def context() -> AsyncGenerator[Context]:
    runtime_engine = create_engine(
        os.getenv(
            "TIRAMISU_DATABASE_URL",
            "postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu_test",
        )
    )
    admin_engine = create_engine(
        os.getenv(
            "TIRAMISU_MIGRATION_DATABASE_URL",
            "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
        )
    )
    context = Context(
        create_session_factory(runtime_engine),
        create_session_factory(admin_engine),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    try:
        async with context.admin.begin() as session:
            for tenant_id in (context.tenant_id, context.other_tenant_id):
                session.add(
                    Tenant(
                        id=tenant_id,
                        slug=f"quarantine-{tenant_id}",
                        name="Quarantine test",
                        deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                    )
                )
            await session.flush()
            for tenant_id, process_id in (
                (context.tenant_id, context.process_id),
                (context.tenant_id, context.second_process_id),
                (context.other_tenant_id, context.other_process_id),
            ):
                session.add(
                    ProcessInstance(
                        id=process_id,
                        tenant_id=tenant_id,
                        workflow_id=f"tenant/{tenant_id}/process/{process_id}",
                        **{
                            field: getattr(bootstrap(), field)
                            for field in bootstrap().__dataclass_fields__
                        },
                    )
                )
        yield context
    finally:
        async with context.admin.begin() as session:
            for table in reversed(Base.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    await session.execute(
                        delete(table).where(
                            table.c.tenant_id.in_((context.tenant_id, context.other_tenant_id))
                        )
                    )
            await session.execute(
                delete(Tenant).where(Tenant.id.in_((context.tenant_id, context.other_tenant_id)))
            )
        await runtime_engine.dispose()
        await admin_engine.dispose()


async def test_unmatched_resolution_is_durable_idempotent_and_establishes_late_correlation(
    context: Context,
) -> None:
    ref = reference("late-thread")
    event = event_for(context, external_references=(ref,))
    await context.ingest(event)
    command = context.command(event, bind_references=(ref,))
    first = await context.resolve(command)
    replay = await context.resolve(command)
    assert first.id == replay.id == command.command_id
    assert first.delivery_scheduled is True
    assert first.previous_reason == "no_process_match"
    async with context.runtime.begin() as session:
        await set_tenant_context(session, context.tenant_id)
        inbox = await session.get(EventInbox, event.event_id)
        assert inbox is not None and inbox.event_data == event.model_dump(mode="json")
        assert inbox.process_instance_id == context.process_id
        assert inbox.received_at == event.received_at
        assert await session.scalar(select(func.count()).select_from(EventResolutionCommand)) == 1
        messages = (await session.scalars(select(OutboxMessage))).all()
        assert len(messages) == 1
        assert messages[0].payload == {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
        }
        assert messages[0].destination == f"tenant/{context.tenant_id}/process/{context.process_id}"
        duplicate = await EventIngestionService().ingest(
            session, event.model_copy(update={"event_id": uuid4()})
        )
        assert duplicate.created is False and duplicate.event_id == event.event_id
        assert duplicate.outbox_message_id == messages[0].id
        later = await EventIngestionService().ingest(
            session, event_for(context, external_references=(ref,))
        )
        assert (
            later.correlation_status == "matched"
            and later.process_instance_id == context.process_id
        )
    for altered in (
        replace(command, reason="Changed reason"),
        replace(command, actor_id=uuid4()),
        replace(command, process_instance_id=context.second_process_id),
        replace(command, bind_references=()),
    ):
        with pytest.raises(QuarantineConflict, match="reused"):
            await context.resolve(altered)
    with pytest.raises(QuarantineConflict, match="already correlated"):
        await context.resolve(replace(command, command_id=uuid4()))


async def test_ambiguous_trigger_stays_quarantined_and_conflicting_references_are_not_reassigned(
    context: Context,
) -> None:
    left, right, unbound = reference("left"), reference("right"), reference("unbound")
    async with context.admin.begin() as session:
        for ref, process_id in ((left, context.process_id), (right, context.second_process_id)):
            session.add(
                ExternalCorrelation(
                    tenant_id=context.tenant_id, process_instance_id=process_id, **ref.model_dump()
                )
            )
    event = event_for(context, external_references=(left, right, unbound))
    async with context.runtime.begin() as session:
        result = await EventIngestionService().ingest(session, event, bootstrap=bootstrap())
        assert result.correlation_reason == "ambiguous_external_references"
        assert result.process_instance_id is None and result.outbox_message_id is None
        assert await session.scalar(select(func.count()).select_from(ProcessInstance)) == 2
    with pytest.raises(QuarantineConflict, match="ownership"):
        await context.resolve(context.command(event, bind_references=(unbound, right)))
    with pytest.raises(QuarantineConflict, match="original event"):
        await context.resolve(context.command(event, bind_references=(reference("invented"),)))
    async with context.runtime.begin() as session:
        await set_tenant_context(session, context.tenant_id)
        assert await session.scalar(select(func.count()).select_from(EventResolutionCommand)) == 0
        assert await session.scalar(select(func.count()).select_from(ExternalCorrelation)) == 2
    await context.resolve(context.command(event, bind_references=(left, unbound)))
    async with context.runtime.begin() as session:
        await set_tenant_context(session, context.tenant_id)
        owners = {
            row.external_id: row.process_instance_id
            for row in (await session.scalars(select(ExternalCorrelation))).all()
        }
        assert owners == {
            "left": context.process_id,
            "right": context.second_process_id,
            "unbound": context.process_id,
        }
        inbox = await session.get(EventInbox, event.event_id)
        assert inbox is not None and inbox.event_data == event.model_dump(mode="json")


@pytest.mark.parametrize("same_command", [True, False])
async def test_concurrent_resolutions_schedule_only_one_delivery(
    context: Context, same_command: bool
) -> None:
    event = event_for(context)
    await context.ingest(event)
    first = context.command(event)
    second = (
        first
        if same_command
        else replace(first, command_id=uuid4(), process_instance_id=context.second_process_id)
    )
    results = await asyncio.gather(
        context.resolve(first), context.resolve(second), return_exceptions=True
    )
    assert sum(isinstance(result, EventResolutionCommand) for result in results) == (
        2 if same_command else 1
    )
    assert sum(isinstance(result, QuarantineConflict) for result in results) == (
        0 if same_command else 1
    )
    async with context.runtime.begin() as session:
        await set_tenant_context(session, context.tenant_id)
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 1
        assert await session.scalar(select(func.count()).select_from(EventResolutionCommand)) == 1


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
async def test_terminal_resolution_records_only(context: Context, status: str) -> None:
    event = event_for(context)
    await context.ingest(event)
    async with context.admin.begin() as session:
        await session.execute(
            update(ProcessInstance)
            .where(ProcessInstance.id == context.process_id)
            .values(status=status)
        )
    stored = await context.resolve(context.command(event))
    assert stored.delivery_scheduled is False
    async with context.runtime.begin() as session:
        await set_tenant_context(session, context.tenant_id)
        inbox = await session.get(EventInbox, event.event_id)
        assert inbox is not None and inbox.correlation_reason == "terminal_process_record_only"
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 0


async def test_resolution_cannot_cross_tenants_or_deployments_or_bypass_suspension(
    context: Context,
) -> None:
    event = event_for(context)
    await context.ingest(event)
    with pytest.raises(QuarantineNotFound):
        await context.resolve(context.command(event, process_instance_id=context.other_process_id))
    with pytest.raises(QuarantineNotFound):
        await context.resolve(
            context.command(
                event,
                tenant_id=context.other_tenant_id,
                process_instance_id=context.other_process_id,
            )
        )
    async with context.runtime.begin() as session:
        with pytest.raises(TenantNotAuthorized):
            await QuarantineResolutionService().resolve(
                session, context.command(event), deployment_id="wrong-deployment"
            )
    async with context.admin.begin() as session:
        await session.execute(
            update(ProcessInstance)
            .where(ProcessInstance.id == context.process_id)
            .values(deployment_id="other")
        )
    with pytest.raises(QuarantineConflict, match="another deployment"):
        await context.resolve(context.command(event))
    async with context.admin.begin() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == context.tenant_id).values(status="suspended")
        )
    with pytest.raises(TenantSuspended):
        await context.resolve(context.command(event))


async def test_delivery_failure_rolls_back_resolution_and_references(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = reference("rollback")
    event = event_for(context, external_references=(ref,))
    await context.ingest(event)
    command = context.command(event, bind_references=(ref,))

    async def fail(*args: Any, **kwargs: Any) -> UUID:
        raise RuntimeError("outbox insert failed")

    with monkeypatch.context() as patch:
        patch.setattr(EventIngestionService, "schedule_delivery", fail)
        with pytest.raises(RuntimeError, match="outbox insert failed"):
            await context.resolve(command)
    async with context.runtime.begin() as session:
        await set_tenant_context(session, context.tenant_id)
        row = await session.get(EventInbox, event.event_id)
        assert row is not None and row.correlation_status == "pending"
        assert await session.scalar(select(func.count()).select_from(EventResolutionCommand)) == 0
        assert await session.scalar(select(func.count()).select_from(ExternalCorrelation)) == 0
    assert (await context.resolve(command)).delivery_scheduled


async def test_ingress_and_late_reference_binding_serialize(context: Context) -> None:
    ref = reference("racing-thread")
    event = event_for(context, external_references=(ref,))
    await context.ingest(event)

    async def ingest_bound() -> None:
        await context.ingest(
            event_for(
                context, process_instance_id=context.second_process_id, external_references=(ref,)
            )
        )

    results = await asyncio.gather(
        context.resolve(context.command(event, bind_references=(ref,))),
        ingest_bound(),
        return_exceptions=True,
    )
    assert all(
        result is None or isinstance(result, EventResolutionCommand | QuarantineConflict)
        for result in results
    )
    async with context.runtime.begin() as session:
        await set_tenant_context(session, context.tenant_id)
        owners = (await session.scalars(select(ExternalCorrelation))).all()
        assert len(owners) == 1
        matched = (
            await session.scalars(
                select(EventInbox).where(EventInbox.correlation_status == "matched")
            )
        ).all()
        assert len(matched) == 1
        assert matched[0].process_instance_id == owners[0].process_instance_id


async def test_authenticated_api_inspection_resolution_history_and_permissions(
    context: Context,
) -> None:
    event = event_for(
        context, external_references=(reference("api-thread"),), process_instance_id=uuid4()
    )
    await context.ingest(event)
    app = create_app(
        settings=Settings(**cast(Any, {"_env_file": None, "environment": "production"})),
        session_factory=context.runtime,
    )
    async with context.admin.begin() as session:
        tokens: dict[str, str] = {}
        for name, tenant_id, scopes in (
            ("reader", context.tenant_id, (CredentialScope.QUARANTINE_READ,)),
            (
                "operator",
                context.tenant_id,
                (CredentialScope.QUARANTINE_READ, CredentialScope.QUARANTINE_RESOLVE),
            ),
            ("ingress", context.tenant_id, (CredentialScope.EVENTS_INGEST,)),
            (
                "other",
                context.other_tenant_id,
                (CredentialScope.QUARANTINE_READ, CredentialScope.QUARANTINE_RESOLVE),
            ),
        ):
            issued = await TenantCredentialService().issue(
                session, tenant_id=tenant_id, actor_id=context.actor_id, name=name, scopes=scopes
            )
            tokens[name] = issued.token

    def auth(name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens[name]}"}

    path = f"/v1/quarantine/{event.event_id}"
    body = {
        "command_id": str(uuid4()),
        "process_instance_id": str(context.process_id),
        "reason": "Checked original case",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/quarantine")).status_code == 401
        assert (await client.get("/v1/quarantine", headers=auth("ingress"))).status_code == 403
        page = (await client.get("/v1/quarantine?limit=1", headers=auth("reader"))).json()
        assert page["total"] == 1 and len(page["items"]) == 1 and page["can_resolve"] is False
        assert (await client.get("/v1/quarantine?offset=1", headers=auth("reader"))).json()[
            "items"
        ] == []
        detail = (await client.get(path, headers=auth("reader"))).json()
        assert detail["event"] == event.model_dump(mode="json")
        assert detail["correlation_reason"] == "explicit_process_not_found"
        assert detail["references"][0]["process_instance_id"] is None
        assert (await client.get(path, headers=auth("other"))).status_code == 404
        assert (await client.get("/v1/quarantine", headers=auth("other"))).json()["total"] == 0
        assert (
            await client.post(path + "/resolve", json=body, headers=auth("reader"))
        ).status_code == 403
        assert (
            await client.post(path + "/resolve", json=body, headers=auth("other"))
        ).status_code == 404
        assert (
            await client.post(
                path + "/resolve",
                json={**body, "process_instance_id": str(context.other_process_id)},
                headers=auth("operator"),
            )
        ).status_code == 404
        assert (
            await client.post(
                path + "/resolve", json={**body, "reason": " "}, headers=auth("operator")
            )
        ).status_code == 409
        assert (
            await client.post(
                path + "/resolve", json={**body, "actor_id": str(uuid4())}, headers=auth("operator")
            )
        ).status_code == 422
        first = await client.post(path + "/resolve", json=body, headers=auth("operator"))
        assert first.status_code == 202, first.text
        again = await client.post(path + "/resolve", json=body, headers=auth("operator"))
        assert again.json() == first.json()
        assert first.json()["actor_id"] == str(context.actor_id)
        assert (
            await client.post(
                path + "/resolve", json={**body, "reason": "Other"}, headers=auth("operator")
            )
        ).status_code == 409
        assert (await client.get("/v1/quarantine", headers=auth("reader"))).json()["total"] == 0
        history = (await client.get("/v1/quarantine?state=resolved", headers=auth("reader"))).json()
        assert history["total"] == 1 and history["items"][0]["resolution"] == first.json()
        final = (await client.get(path, headers=auth("reader"))).json()
        assert final["event"] == detail["event"]
        assert final["resolution"] == first.json()
        assert final["candidates"][0]["id"] == str(context.process_id)


class LostResponseClient:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.calls = 0

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        await self.client.start_workflow(*args, **kwargs)
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("response lost after Temporal accepted the signal")


async def test_resolved_event_uses_real_mailbox_deduplication_after_lost_dispatch_response(
    context: Context,
) -> None:
    event = event_for(context)
    await context.ingest(event)
    command = context.command(event)
    await context.resolve(command)
    workflow_id = f"tenant/{context.tenant_id}/process/{context.process_id}"
    async with (
        await start_time_skipping_environment() as environment,
        Worker(
            environment.client,
            task_queue=TEST_DEPLOYMENT_RELEASE.temporal_task_queue,
            workflows=[ProcessMailboxWorkflow],
        ),
    ):
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(
                tenant_id=str(context.tenant_id), process_instance_id=str(context.process_id)
            ),
            id=workflow_id,
            task_queue=TEST_DEPLOYMENT_RELEASE.temporal_task_queue,
        )
        await handle.signal(
            ProcessMailboxWorkflow.replace_wake_plan, WakePlan(event_types=(event.event_type,))
        )
        lost_response = LostResponseClient(environment.client)
        dispatcher = TemporalOutboxDispatcher(
            context.runtime,
            cast(Client, lost_response),
            deployment_release=TEST_DEPLOYMENT_RELEASE,
            authorized_tenant_ids=frozenset({context.tenant_id}),
            retry_base_delay=timedelta(0),
        )
        assert (
            await dispatcher.dispatch_one(context.tenant_id)
        ).status is DispatchStatus.RETRY_SCHEDULED
        # Reconstruct both services, as after a restart, and retry the exact resolution.
        await context.resolve(command)
        dispatcher = TemporalOutboxDispatcher(
            context.runtime,
            environment.client,
            deployment_release=TEST_DEPLOYMENT_RELEASE,
            authorized_tenant_ids=frozenset({context.tenant_id}),
        )
        assert (await dispatcher.dispatch_one(context.tenant_id)).status is DispatchStatus.PUBLISHED
        assert (await dispatcher.dispatch_one(context.tenant_id)).status is DispatchStatus.EMPTY
        state = await handle.query(ProcessMailboxWorkflow.state)
        assert len(state.wake_records) == 1
        assert state.wake_records[0].event_id == str(event.event_id)
        await handle.signal(ProcessMailboxWorkflow.close)
        await handle.result()
