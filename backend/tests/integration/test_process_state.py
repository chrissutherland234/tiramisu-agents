"""Durable process knowledge, memory, and lifecycle integration tests."""

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tiramisu_agents.agents.context import PostgresAgentContextLoader
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.decisions import (
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    MemoryUpdate,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.contracts.processes import ProcessStatus
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance, ProcessStateRevision
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.compatibility import DeploymentCompatibility
from tiramisu_agents.processes.state import ProcessStateConflict, ProcessStateService
from tiramisu_agents.testkit.deployment import TEST_DEPLOYMENT_RELEASE

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        await session.execute(
            delete(ProcessStateRevision).where(ProcessStateRevision.tenant_id == tenant_id)
        )
        await session.execute(delete(OutboxMessage).where(OutboxMessage.tenant_id == tenant_id))
        await session.execute(delete(EventInbox).where(EventInbox.tenant_id == tenant_id))
        await session.execute(
            delete(ExternalCorrelation).where(ExternalCorrelation.tenant_id == tenant_id)
        )
        await session.execute(delete(ProcessInstance).where(ProcessInstance.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_process_state_projects_sourced_knowledge_and_versioned_memory() -> None:
    runtime_url = os.getenv(
        "TIRAMISU_DATABASE_URL",
        "postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu_test",
    )
    migration_url = os.getenv(
        "TIRAMISU_MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
    )
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    deployment = load_fictional_deployment()
    definition = deployment.definition
    tenant_id = uuid4()
    reference = ExternalReference(
        provider="stub.website",
        resource_type="enquiry",
        external_id=f"enquiry-{uuid4()}",
    )
    enquiry = CanonicalEvent(
        tenant_id=tenant_id,
        event_type="enquiry.created",
        source="stub.website",
        source_event_id=f"source-{uuid4()}",
        occurred_at=datetime.now(UTC),
        external_references=(reference,),
        facts=(
            FactObservation(
                key="customer.identifier",
                kind=FactKind.AUTHORITATIVE,
                value="customer-123",
            ),
            FactObservation(
                key="customer.initial_request",
                kind=FactKind.CUSTOMER_CLAIM,
                value="Tuesday afternoon, please",
            ),
        ),
    )
    service = ProcessStateService()

    try:
        async with admin_factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"tenant-{tenant_id}",
                    name="Test Tenant",
                    deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                )
            )
        async with runtime_factory.begin() as session:
            ingested = await EventIngestionService().ingest(
                session,
                enquiry,
                bootstrap=ProcessBootstrap(
                    process_type="enquiry_to_booking",
                    definition_version=definition.version,
                    extension_manifest_hash="a" * 64,
                    client_pack_fingerprint="b" * 64,
                    process_definition_fingerprint=definition.fingerprint(),
                    deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                    deployment_release_fingerprint=TEST_DEPLOYMENT_RELEASE.release_fingerprint,
                    temporal_task_queue=TEST_DEPLOYMENT_RELEASE.temporal_task_queue,
                ),
            )
        assert ingested.process_instance_id is not None
        process_id = ingested.process_instance_id
        first_turn_id = uuid4()
        first_decision = AgentDecision(
            based_on_event_ids=(enquiry.event_id,),
            status=DecisionStatus.WAITING,
            wake_conditions=(EventWakeCondition(event_type="booking.confirmed"),),
            memory_update=MemoryUpdate(
                summary="The customer wants a Tuesday afternoon booking.",
                summary_source_event_ids=(enquiry.event_id,),
                open_commitments=("Confirm a suitable Tuesday slot",),
            ),
        )
        async with runtime_factory.begin() as session:
            first = await service.apply_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                agent_turn_id=first_turn_id,
                decision=first_decision,
            )
        assert first.version == 1
        assert first.status is ProcessStatus.WAITING

        # Activity retries are idempotent and do not create another revision.
        async with runtime_factory.begin() as session:
            retried = await service.apply_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                agent_turn_id=first_turn_id,
                decision=first_decision,
            )
        assert retried == first

        premature_completion = AgentDecision(
            based_on_event_ids=(),
            status=DecisionStatus.COMPLETED,
        )
        with pytest.raises(ProcessStateConflict, match="booking.status"):
            async with runtime_factory.begin() as session:
                await service.apply_decision(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    agent_turn_id=uuid4(),
                    decision=premature_completion,
                    completion_requirements={"booking.status": "confirmed"},
                )

        confirmation = CanonicalEvent(
            tenant_id=tenant_id,
            event_type="booking.confirmed",
            source="stub.booking",
            source_event_id=f"confirmation-{uuid4()}",
            occurred_at=datetime.now(UTC),
            external_references=(reference,),
            facts=(
                FactObservation(
                    key="booking.status",
                    kind=FactKind.AUTHORITATIVE,
                    value="confirmed",
                ),
            ),
        )
        async with runtime_factory.begin() as session:
            await EventIngestionService().ingest(session, confirmation)
        completed_decision = AgentDecision(
            based_on_event_ids=(confirmation.event_id,),
            status=DecisionStatus.COMPLETED,
        )
        async with runtime_factory.begin() as session:
            completed = await service.apply_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                agent_turn_id=uuid4(),
                decision=completed_decision,
                completion_requirements={"booking.status": "confirmed"},
            )
        assert completed.version == 2
        assert completed.status is ProcessStatus.COMPLETED

        async with runtime_factory.begin() as session:
            context = await PostgresAgentContextLoader().load(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                turn_id=uuid4(),
                event_ids=(confirmation.event_id,),
                definition=definition,
                compatibility=DeploymentCompatibility(
                    client_pack_fingerprint="b" * 64,
                    extension_manifest_hash="a" * 64,
                    definition_fingerprints={
                        (definition.id, definition.version): definition.fingerprint()
                    },
                ),
                deployment_release=TEST_DEPLOYMENT_RELEASE,
            )
            revisions = (
                await session.scalars(
                    select(ProcessStateRevision)
                    .where(ProcessStateRevision.process_instance_id == process_id)
                    .order_by(ProcessStateRevision.version)
                )
            ).all()

        assert context.process.status is ProcessStatus.COMPLETED
        assert context.process.state_version == 2
        assert context.process.authoritative_facts == {
            "customer.identifier": "customer-123",
            "booking.status": "confirmed",
        }
        assert context.process.customer_claims == {
            "customer.initial_request": "Tuesday afternoon, please"
        }
        assert context.process.fact_provenance["authoritative:booking.status"] == {
            "kind": "authoritative",
            "source_type": "event",
            "source_id": str(confirmation.event_id),
        }
        assert context.process.memory_summary == ("The customer wants a Tuesday afternoon booking.")
        assert context.process.memory_summary_source_event_ids == (enquiry.event_id,)
        assert context.process.open_commitments == ()
        assert context.process.current_wake_conditions == ()
        assert [revision.version for revision in revisions] == [1, 2]
        assert revisions[0].open_commitments == ["Confirm a suitable Tuesday slot"]
        assert revisions[0].wake_conditions == [
            {"type": "event", "event_type": "booking.confirmed"}
        ]
        assert revisions[1].memory_summary == revisions[0].memory_summary

        with pytest.raises(ProcessStateConflict, match="terminal process state"):
            async with runtime_factory.begin() as session:
                await service.apply_decision(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    agent_turn_id=uuid4(),
                    decision=completed_decision.model_copy(update={"decision_id": uuid4()}),
                )
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
