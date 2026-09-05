"""Action proposal durability and tenant isolation against PostgreSQL."""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from tiramisu_agents.actions.execution import ActionExecutionRejected, ActionExecutor
from tiramisu_agents.actions.gateway import ActionGateway, ActionPersistenceConflict
from tiramisu_agents.actions.reconciliation import (
    ActionReconciliationService,
    ActionResolutionConflict,
)
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.adapters.stubs import StubActionAdapter, StubAmbiguousSuccess
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.actions import (
    ActionConflict,
    ActionResolution,
    OperatorActionResolution,
    PermissionOutcome,
)
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.core.ports.actions import (
    AmbiguousActionOutcome,
    DefinitiveActionConflict,
    ProviderActionResult,
)
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionReconciliationDecision,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance, ProcessStateRevision
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import Tenant, TenantSafetyEvent
from tiramisu_agents.db.models.usage import ModelUsageLedger
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.compatibility import DeploymentCompatibility
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.processes.state import ProcessStateService
from tiramisu_agents.reviews.service import ReviewConflict, ReviewService
from tiramisu_agents.security.tenancy import TenantSafetyService
from tiramisu_agents.temporal.activities.action_execution import (
    ActionExecutionActivities,
    ExecuteActionCommand,
)
from tiramisu_agents.temporal.activities.action_gateway import (
    ActionGatewayActivities,
    PersistActionsCommand,
)
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities, AgentTurnCommand
from tiramisu_agents.temporal.dispatcher import DispatchStatus, TemporalOutboxDispatcher
from tiramisu_agents.testkit.deployment import TEST_DEPLOYMENT_RELEASE
from tiramisu_agents.testkit.scripted_agent import ScriptedAgent

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


class _CapturingTemporalClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_workflow(self, *_: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        await session.execute(
            delete(ProcessStateRevision).where(ProcessStateRevision.tenant_id == tenant_id)
        )
        for model in (
            ModelUsageLedger,
            ActionReconciliationDecision,
            ActionAttempt,
            ApprovalDecision,
            ReviewMessage,
            ReviewThread,
            ApprovalRequest,
            ActionPolicyRecord,
            ActionRevision,
            ActionRequest,
        ):
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.execute(delete(OutboxMessage).where(OutboxMessage.tenant_id == tenant_id))
        await session.execute(delete(EventInbox).where(EventInbox.tenant_id == tenant_id))
        await session.execute(
            delete(ExternalCorrelation).where(ExternalCorrelation.tenant_id == tenant_id)
        )
        await session.execute(delete(ProcessInstance).where(ProcessInstance.tenant_id == tenant_id))
        await session.execute(
            delete(TenantSafetyEvent).where(TenantSafetyEvent.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_gateway_is_idempotent_hash_bound_and_tenant_isolated() -> None:
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
    definition = load_fictional_deployment().definition
    compatibility = DeploymentCompatibility(
        client_pack_fingerprint="b" * 64,
        extension_manifest_hash="a" * 64,
        definition_fingerprints={(definition.id, definition.version): definition.fingerprint()},
    )
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    turn_id = uuid4()
    event = CanonicalEvent(
        tenant_id=tenant_id,
        event_type="enquiry.created",
        source="stub.website",
        source_event_id=f"source-{uuid4()}",
        occurred_at=datetime.now(UTC),
        external_references=(
            ExternalReference(
                provider="stub.website",
                resource_type="enquiry",
                external_id=f"enquiry-{uuid4()}",
            ),
        ),
    )
    decision = AgentDecision(
        based_on_event_ids=(event.event_id,),
        status=DecisionStatus.ACTIVE,
        actions=(
            ActionProposal(
                logical_action_key="reply_1",
                action_type="send_message",
                parameters={"recipient": "customer@example.test", "body": "Hello"},
                rationale="Respond to the enquiry.",
            ),
            ActionProposal(
                logical_action_key="availability_1",
                action_type="find_available_slots",
                parameters={"days": 7},
                rationale="Find suitable times.",
            ),
            ActionProposal(
                logical_action_key="booking_1",
                action_type="propose_booking",
                parameters={"slot": "2026-09-02T10:00:00+00:00"},
                rationale="Offer an available time.",
            ),
            ActionProposal(
                logical_action_key="availability_unknown_1",
                action_type="find_available_slots",
                parameters={"days": 14},
                rationale="Exercise unresolved provider reconciliation.",
            ),
        ),
    )
    gateway = ActionGateway()

    try:
        async with admin_factory.begin() as session:
            session.add_all(
                [
                    Tenant(
                        id=tenant_id,
                        slug=f"tenant-{tenant_id}",
                        name="Gateway Tenant",
                        deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                    ),
                    Tenant(
                        id=other_tenant_id,
                        slug=f"tenant-{other_tenant_id}",
                        name="Other Tenant",
                        deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                    ),
                ]
            )
        async with runtime_factory.begin() as session:
            ingested = await EventIngestionService().ingest(
                session,
                event,
                bootstrap=ProcessBootstrap(
                    process_type=definition.id,
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

        async with runtime_factory.begin() as session:
            first = await gateway.persist_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                agent_turn_id=turn_id,
                process_definition_version=definition.version,
                decision=decision,
                policy=definition.action_policy(),
            )
        async with runtime_factory.begin() as session:
            retried = await gateway.persist_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                agent_turn_id=turn_id,
                process_definition_version=definition.version,
                decision=decision,
                policy=definition.action_policy(),
            )

        assert retried == first
        assert first[0].outcome is PermissionOutcome.REQUIRE_APPROVAL
        assert first[0].approval_request_id is not None
        assert first[0].review_thread_id is not None
        assert first[1].outcome is PermissionOutcome.ALLOW
        assert first[1].approval_request_id is None

        async with runtime_factory.begin() as session:
            projected = await ProcessStateService().apply_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                agent_turn_id=turn_id,
                decision=decision,
            )
        assert projected.status == "review"

        activity_result = await ActionGatewayActivities(
            runtime_factory,
            ProcessDefinitionRegistry([definition]),
            deployment_release=TEST_DEPLOYMENT_RELEASE,
        ).persist_agent_actions(
            PersistActionsCommand(
                tenant_id=str(tenant_id),
                process_instance_id=str(ingested.process_instance_id),
                process_definition_id=definition.id,
                process_definition_version=definition.version,
                agent_turn_id=str(turn_id),
                event_ids=(str(event.event_id),),
                workflow_now=datetime.now(UTC),
                decision_json=decision.model_copy(
                    update={"actions": decision.actions[:3]}
                ).model_dump_json(),
            )
        )
        assert len(json.loads(activity_result.actions_json)) == 3

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            assert await session.scalar(select(func.count()).select_from(ActionRequest)) == 4
            assert await session.scalar(select(func.count()).select_from(ActionRevision)) == 4
            assert await session.scalar(select(func.count()).select_from(ActionPolicyRecord)) == 4
            assert await session.scalar(select(func.count()).select_from(ApprovalRequest)) == 2
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, other_tenant_id)
            assert await session.scalar(select(func.count()).select_from(ActionRequest)) == 0

        actor_id = uuid4()
        comment = ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            review_thread_id=first[0].review_thread_id,
            action_request_id=first[0].action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.COMMENT,
            actor_id=actor_id,
            message="Could this be a little warmer?",
        )
        review_service = ReviewService()
        async with runtime_factory.begin() as session:
            comment_result = await review_service.apply(session, comment)
        async with runtime_factory.begin() as session:
            assert await review_service.apply(session, comment) == comment_result
        assert comment_result.thread_status == "open"

        wrong_hash = ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            review_thread_id=first[0].review_thread_id,
            action_request_id=first[0].action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.APPROVE,
            actor_id=actor_id,
            expected_payload_hash="0" * 64,
        )
        with pytest.raises(ReviewConflict, match="payload hash"):
            async with runtime_factory.begin() as session:
                await review_service.apply(session, wrong_hash)

        approve = wrong_hash.model_copy(
            update={"command_id": uuid4(), "expected_payload_hash": first[0].payload_hash}
        )
        assert first[0].approval_request_id is not None
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            expiring_approval = await session.get(
                ApprovalRequest,
                first[0].approval_request_id,
            )
            assert expiring_approval is not None
            expiring_approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(ReviewConflict, match="approval has expired"):
            async with runtime_factory.begin() as session:
                await review_service.apply(session, approve)
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            expiring_approval = await session.get(
                ApprovalRequest,
                first[0].approval_request_id,
            )
            assert expiring_approval is not None
            expiring_approval.expires_at = datetime.now(UTC) + timedelta(hours=1)
        async with runtime_factory.begin() as session:
            approved = await review_service.apply(session, approve)
        async with runtime_factory.begin() as session:
            assert await review_service.apply(session, approve) == approved
        assert approved.approval_status == "approved"
        assert approved.action_status == "approved"

        messaging_adapter = StubActionAdapter()
        availability_result = ProviderActionResult(
            provider_reference="stub:availability-1",
            result={"slots": ["2026-09-02T14:00:00+00:00"]},
            facts=(
                FactObservation(
                    key="booking.available_slots",
                    kind=FactKind.AUTHORITATIVE,
                    value=["2026-09-02T14:00:00+00:00"],
                ),
            ),
        )
        availability_adapter = StubActionAdapter(
            [
                StubAmbiguousSuccess(availability_result),
                AmbiguousActionOutcome("provider timed out with no lookup record"),
                DefinitiveActionConflict(
                    ActionConflict(
                        code="resource_unavailable",
                        message="the requested resource is no longer available",
                        details={"resource_type": "appointment_slot"},
                        facts=(
                            FactObservation(
                                key="booking.available_slots",
                                kind=FactKind.AUTHORITATIVE,
                                value=["2026-09-04T14:00:00+00:00"],
                            ),
                        ),
                    )
                ),
            ]
        )
        executor = ActionExecutor(
            runtime_factory,
            ActionAdapterRegistry(
                {
                    "send_message": messaging_adapter,
                    "find_available_slots": availability_adapter,
                    "propose_booking": StubActionAdapter(),
                }
            ),
            compatibility,
            TEST_DEPLOYMENT_RELEASE,
            ProcessDefinitionRegistry([definition]),
        )
        async with admin_factory.begin() as session:
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            process.client_pack_fingerprint = "d" * 64
        with pytest.raises(ApplicationError) as incompatible:
            await ActionExecutionActivities(executor).execute_action(
                ExecuteActionCommand(
                    tenant_id=str(tenant_id),
                    process_instance_id=str(ingested.process_instance_id),
                    action_request_id=str(first[1].action_request_id),
                    revision=1,
                )
            )
        assert incompatible.value.type == "DeploymentCompatibilityError"
        assert incompatible.value.non_retryable is True
        assert availability_adapter.requests == []
        async with admin_factory.begin() as session:
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            process.client_pack_fingerprint = "b" * 64

        async with admin_factory.begin() as session:
            await TenantSafetyService().set_status(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                new_status="suspended",
                reason="Block side effects while a provider incident is investigated",
            )
        with pytest.raises(ActionExecutionRejected, match="safety control"):
            await executor.execute(
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                action_request_id=first[1].action_request_id,
                revision=1,
            )
        assert availability_adapter.requests == []
        async with admin_factory.begin() as session:
            await TenantSafetyService().set_status(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                new_status="active",
                reason="Provider incident resolved",
            )

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            expiring_approval = await session.get(
                ApprovalRequest,
                first[0].approval_request_id,
            )
            assert expiring_approval is not None
            expiring_approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(ActionExecutionRejected, match="approval has expired"):
            await executor.execute(
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                action_request_id=first[0].action_request_id,
                revision=1,
            )
        assert messaging_adapter.requests == []
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            expiring_approval = await session.get(
                ApprovalRequest,
                first[0].approval_request_id,
            )
            assert expiring_approval is not None
            expiring_approval.expires_at = datetime.now(UTC) + timedelta(hours=1)

        sent = await executor.execute(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=first[0].action_request_id,
            revision=1,
        )
        sent_retry = await executor.execute(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=first[0].action_request_id,
            revision=1,
        )
        assert sent_retry == sent
        assert sent.status == "succeeded"
        assert len(messaging_adapter.requests) == 1

        ambiguous = await executor.execute(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=first[1].action_request_id,
            revision=1,
        )
        reconciled = await executor.execute(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=first[1].action_request_id,
            revision=1,
        )
        assert ambiguous.status == "unknown"
        assert reconciled.status == "succeeded"
        assert reconciled.provider_reference == availability_result.provider_reference
        assert reconciled.facts == availability_result.facts
        assert len(availability_adapter.requests) == 1

        unresolved = await executor.execute(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=first[3].action_request_id,
            revision=1,
        )
        still_unknown = await executor.reconcile(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=first[3].action_request_id,
            revision=1,
        )
        assert unresolved.status == "unknown"
        assert still_unknown.status == "unknown"
        assert len(availability_adapter.requests) == 2

        conflict_turn_id = uuid4()
        conflict_decision = AgentDecision(
            based_on_event_ids=(event.event_id,),
            status=DecisionStatus.ACTIVE,
            actions=(
                ActionProposal(
                    logical_action_key="availability_conflict_1",
                    action_type="find_available_slots",
                    parameters={"days": 21},
                    rationale="Exercise a definitive provider resource conflict.",
                ),
            ),
        )
        async with runtime_factory.begin() as session:
            conflict_action = (
                await gateway.persist_decision(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=ingested.process_instance_id,
                    agent_turn_id=conflict_turn_id,
                    process_definition_version=definition.version,
                    decision=conflict_decision,
                    policy=definition.action_policy(),
                )
            )[0]
        conflict_result = await executor.execute(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=conflict_action.action_request_id,
            revision=1,
        )
        conflict_retry = await executor.execute(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=conflict_action.action_request_id,
            revision=1,
        )
        assert conflict_result == conflict_retry
        assert conflict_result.status == "conflict"
        assert conflict_result.conflict is not None
        assert conflict_result.conflict.code == "resource_unavailable"
        assert conflict_result.facts[0].value == ["2026-09-04T14:00:00+00:00"]
        assert len(availability_adapter.requests) == 3

        # Model a platform crash after the provider recorded its conflict but
        # before the terminal database update. Lookup must recover the conflict;
        # the provider operation itself must not run again.
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            attempt = await session.get(ActionAttempt, conflict_result.attempt_id)
            request = await session.get(ActionRequest, conflict_action.action_request_id)
            assert attempt is not None
            assert request is not None
            attempt.status = "executing"
            attempt.conflict = None
            attempt.facts = []
            attempt.error = None
            attempt.completed_at = None
            request.status = "executing"
        recovered_conflict = await executor.execute(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_request_id=conflict_action.action_request_id,
            revision=1,
        )
        assert recovered_conflict == conflict_result
        assert len(availability_adapter.requests) == 3

        repeated_conflict_action = AgentDecision(
            based_on_event_ids=(),
            based_on_action_attempt_ids=(conflict_result.attempt_id,),
            status=DecisionStatus.ACTIVE,
            actions=(
                ActionProposal(
                    logical_action_key="availability_conflict_unchanged",
                    action_type="find_available_slots",
                    parameters={"days": 21},
                    rationale="Incorrectly repeat the action without changing it.",
                ),
            ),
        )
        conflict_follow_up = AgentDecision(
            based_on_event_ids=(),
            based_on_action_attempt_ids=(conflict_result.attempt_id,),
            status=DecisionStatus.WAITING,
            wake_conditions=(EventWakeCondition(event_type="customer.email_received"),),
        )
        conflict_agent = ScriptedAgent([repeated_conflict_action, conflict_follow_up])
        conflict_turn = await AgentTurnActivities(
            runtime_factory,
            ProcessDefinitionRegistry([definition]),
            conflict_agent,
            compatibility=compatibility,
            deployment_release=TEST_DEPLOYMENT_RELEASE,
        ).run_agent_turn(
            AgentTurnCommand(
                tenant_id=str(tenant_id),
                process_instance_id=str(ingested.process_instance_id),
                process_definition_id=definition.id,
                process_definition_version=definition.version,
                turn_id=str(uuid4()),
                event_ids=(),
                workflow_now=datetime.now(UTC),
                action_attempt_ids=(str(conflict_result.attempt_id),),
            )
        )
        assert conflict_turn.proposal_attempt_count == 2
        assert conflict_agent.corrections[1] is not None
        assert "just returned a definitive conflict" in (
            conflict_agent.corrections[1].validation_error
        )
        follow_up_result = conflict_agent.turn_inputs[0].action_results[0]
        assert follow_up_result.status == "conflict"
        assert follow_up_result.conflict == conflict_result.conflict
        assert conflict_agent.turn_inputs[1] == conflict_agent.turn_inputs[0]

        with pytest.raises(ActionPersistenceConflict, match="definitive conflict"):
            async with runtime_factory.begin() as session:
                await gateway.persist_decision(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=ingested.process_instance_id,
                    agent_turn_id=uuid4(),
                    process_definition_version=definition.version,
                    decision=repeated_conflict_action,
                    policy=definition.action_policy(),
                )
        assert len(availability_adapter.requests) == 3
        async with runtime_factory.begin() as session:
            await ProcessStateService().apply_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                agent_turn_id=uuid4(),
                decision=conflict_follow_up,
            )
            await set_tenant_context(session, tenant_id)
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            assert process.authoritative_facts["booking.available_slots"] == [
                "2026-09-04T14:00:00+00:00"
            ]

        operator_resolution = OperatorActionResolution(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            action_attempt_id=unresolved.attempt_id,
            actor_id=actor_id,
            resolution=ActionResolution.SUCCEEDED,
            evidence="Provider support confirmed booking reference availability-manual-1.",
            provider_reference="availability-manual-1",
            result={"slots": ["2026-09-03T10:00:00+00:00"]},
        )
        reconciliation_service = ActionReconciliationService()
        async with runtime_factory.begin() as session:
            operator_resolved = await reconciliation_service.resolve_unknown(
                session, operator_resolution
            )
        async with runtime_factory.begin() as session:
            assert (
                await reconciliation_service.resolve_unknown(session, operator_resolution)
                == operator_resolved
            )
        assert operator_resolved.status == "succeeded"
        with pytest.raises(ActionResolutionConflict, match="different"):
            async with runtime_factory.begin() as session:
                await reconciliation_service.resolve_unknown(
                    session,
                    operator_resolution.model_copy(
                        update={"decision_id": uuid4(), "evidence": "Conflicting evidence."}
                    ),
                )

        result_turn_id = uuid4()
        result_decision = AgentDecision(
            based_on_event_ids=(),
            based_on_action_attempt_ids=(unresolved.attempt_id,),
            status=DecisionStatus.ACTIVE,
            actions=(
                ActionProposal(
                    logical_action_key="availability_after_resolution",
                    action_type="find_available_slots",
                    parameters={"days": 2},
                    rationale="Continue after the confirmed provider outcome.",
                ),
            ),
        )
        result_agent = ScriptedAgent([result_decision])
        result_turn = await AgentTurnActivities(
            runtime_factory,
            ProcessDefinitionRegistry([definition]),
            result_agent,
            compatibility=compatibility,
            deployment_release=TEST_DEPLOYMENT_RELEASE,
        ).run_agent_turn(
            AgentTurnCommand(
                tenant_id=str(tenant_id),
                process_instance_id=str(ingested.process_instance_id),
                process_definition_id=definition.id,
                process_definition_version=definition.version,
                turn_id=str(result_turn_id),
                event_ids=(),
                workflow_now=datetime.now(UTC),
                action_attempt_ids=(str(unresolved.attempt_id),),
            )
        )
        assert result_agent.turn_inputs[0].action_results[0].operator_evidence
        await ActionGatewayActivities(
            runtime_factory,
            ProcessDefinitionRegistry([definition]),
            deployment_release=TEST_DEPLOYMENT_RELEASE,
        ).persist_agent_actions(
            PersistActionsCommand(
                tenant_id=str(tenant_id),
                process_instance_id=str(ingested.process_instance_id),
                process_definition_id=definition.id,
                process_definition_version=definition.version,
                agent_turn_id=str(result_turn_id),
                event_ids=(),
                workflow_now=datetime.now(UTC),
                decision_json=result_turn.decision_json,
                action_attempt_ids=(str(unresolved.attempt_id),),
            )
        )
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            result_revision = await session.scalar(
                select(ActionRevision)
                .join(ActionRequest, ActionRequest.id == ActionRevision.action_request_id)
                .where(ActionRequest.agent_turn_id == result_turn_id)
            )
            assert result_revision is not None
            assert result_revision.based_on_action_attempt_ids == [str(unresolved.attempt_id)]

        with pytest.raises(ActionExecutionRejected, match="not been approved"):
            await executor.execute(
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                action_request_id=first[2].action_request_id,
                revision=1,
            )

        late_revision = ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            review_thread_id=first[0].review_thread_id,
            action_request_id=first[0].action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.REQUEST_REVISION,
            actor_id=actor_id,
            message="Actually, offer Tuesday instead.",
        )
        with pytest.raises(ReviewConflict, match="no longer pending"):
            async with runtime_factory.begin() as session:
                await review_service.apply(session, late_revision)

        assert first[2].review_thread_id is not None
        request_revision = ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            review_thread_id=first[2].review_thread_id,
            action_request_id=first[2].action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.REQUEST_REVISION,
            actor_id=actor_id,
            message="Offer Tuesday afternoon instead.",
        )
        async with runtime_factory.begin() as session:
            revision_result = await review_service.apply(session, request_revision)
        assert revision_result.thread_status == "revision_requested"
        assert revision_result.approval_status == "superseded"
        assert revision_result.action_status == "superseded"

        replacement_turn_id = uuid4()
        replacement_decision = AgentDecision(
            based_on_event_ids=(),
            based_on_review_command_ids=(request_revision.command_id,),
            status=DecisionStatus.ACTIVE,
            actions=(
                ActionProposal(
                    logical_action_key="booking_1_revision_2",
                    action_type="propose_booking",
                    parameters={"slot": "2026-09-02T14:00:00+00:00"},
                    rationale="Apply the operator's requested afternoon slot.",
                ),
            ),
        )
        scripted_agent = ScriptedAgent([replacement_decision])
        turn_result = await AgentTurnActivities(
            runtime_factory,
            ProcessDefinitionRegistry([definition]),
            scripted_agent,
            compatibility=compatibility,
            deployment_release=TEST_DEPLOYMENT_RELEASE,
        ).run_agent_turn(
            AgentTurnCommand(
                tenant_id=str(tenant_id),
                process_instance_id=str(ingested.process_instance_id),
                process_definition_id=definition.id,
                process_definition_version=definition.version,
                turn_id=str(replacement_turn_id),
                event_ids=(),
                workflow_now=datetime.now(UTC),
                review_command_ids=(str(request_revision.command_id),),
            )
        )
        assert scripted_agent.turn_inputs[0].reviews[0].message == request_revision.message
        assert (
            scripted_agent.turn_inputs[0].reviews[0].proposal_parameters
            == decision.actions[2].parameters
        )

        replacement_result = await ActionGatewayActivities(
            runtime_factory,
            ProcessDefinitionRegistry([definition]),
            deployment_release=TEST_DEPLOYMENT_RELEASE,
        ).persist_agent_actions(
            PersistActionsCommand(
                tenant_id=str(tenant_id),
                process_instance_id=str(ingested.process_instance_id),
                process_definition_id=definition.id,
                process_definition_version=definition.version,
                agent_turn_id=str(replacement_turn_id),
                event_ids=(),
                review_command_ids=(str(request_revision.command_id),),
                workflow_now=datetime.now(UTC),
                decision_json=turn_result.decision_json,
            )
        )
        replacement_actions = json.loads(replacement_result.actions_json)
        assert len(replacement_actions) == 1
        assert replacement_actions[0]["outcome"] == "require_approval"
        assert replacement_actions[0]["review_thread_id"] is not None
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            replacement_request = await session.get(
                ActionRequest,
                UUID(replacement_actions[0]["action_request_id"]),
            )
            assert replacement_request is not None
            assert replacement_request.supersedes_action_request_id == first[2].action_request_id

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            assert await session.scalar(select(func.count()).select_from(ReviewMessage)) == 3
            assert await session.scalar(select(func.count()).select_from(ApprovalDecision)) == 1
            assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 5

        temporal_client = _CapturingTemporalClient()
        dispatcher = TemporalOutboxDispatcher(
            runtime_factory,
            cast(Client, temporal_client),
            deployment_release=TEST_DEPLOYMENT_RELEASE,
            authorized_tenant_ids=frozenset({tenant_id}),
        )
        dispatches = [await dispatcher.dispatch_one(tenant_id) for _ in range(5)]
        assert all(item.status is DispatchStatus.PUBLISHED for item in dispatches)
        assert [call["start_signal"] for call in temporal_client.calls] == [
            "receive_event",
            "receive_review",
            "receive_review",
            "receive_action_resolution",
            "receive_review",
        ]

        changed = decision.model_copy(
            update={
                "actions": (
                    decision.actions[0].model_copy(
                        update={"parameters": {"recipient": "other@example.test", "body": "Hello"}}
                    ),
                )
            }
        )
        with pytest.raises(ActionPersistenceConflict, match="another payload"):
            async with runtime_factory.begin() as session:
                await gateway.persist_decision(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=ingested.process_instance_id,
                    agent_turn_id=turn_id,
                    process_definition_version=definition.version,
                    decision=changed,
                    policy=definition.action_policy(),
                )

        async with admin_factory.begin() as session:
            tenant = await session.get(Tenant, tenant_id)
            assert tenant is not None
            tenant.deployment_id = "different-deployment"
        with pytest.raises(ApplicationError) as assignment_error:
            await ActionGatewayActivities(
                runtime_factory,
                ProcessDefinitionRegistry([definition]),
                deployment_release=TEST_DEPLOYMENT_RELEASE,
                authorized_tenant_ids=frozenset({tenant_id}),
            ).persist_agent_actions(
                PersistActionsCommand(
                    tenant_id=str(tenant_id),
                    process_instance_id=str(ingested.process_instance_id),
                    process_definition_id=definition.id,
                    process_definition_version=definition.version,
                    agent_turn_id=str(uuid4()),
                    event_ids=(str(event.event_id),),
                    workflow_now=datetime.now(UTC),
                    decision_json=changed.model_dump_json(),
                )
            )
        assert assignment_error.value.type == "TenantNotAuthorized"
        assert assignment_error.value.non_retryable is True
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await _delete_tenant_data(admin_factory, other_tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
