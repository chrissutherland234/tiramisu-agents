"""Tenant-scoped operator process and review API integration."""

import os
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tiramisu_agents.actions.gateway import ActionGateway
from tiramisu_agents.api.main import create_app
from tiramisu_agents.api.settings import Settings
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    HumanWakeCondition,
    MemoryUpdate,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionReconciliationDecision,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import (
    ProcessControlCommand,
    ProcessInstance,
    ProcessIntervention,
    ProcessStateRevision,
)
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import Tenant, TenantCredential
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.control import InterventionInput, ProcessControlService
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.processes.state import ProcessStateService
from tiramisu_agents.security.credential_service import TenantCredentialService
from tiramisu_agents.security.credentials import CredentialScope
from tiramisu_agents.testkit.deployment import TEST_DEPLOYMENT_RELEASE

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


def _settings(**values: object) -> Settings:
    return Settings(**cast(Any, {"_env_file": None, **values}))


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        for model in (
            ProcessStateRevision,
            ProcessControlCommand,
            ProcessIntervention,
            ActionReconciliationDecision,
            ActionAttempt,
            ApprovalDecision,
            ReviewMessage,
            ReviewThread,
            ApprovalRequest,
            ActionPolicyRecord,
            ActionRevision,
            ActionRequest,
            OutboxMessage,
            EventInbox,
            ExternalCorrelation,
            ProcessInstance,
        ):
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.execute(
            delete(TenantCredential).where(TenantCredential.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_operator_can_inspect_process_and_approve_exact_proposal() -> None:
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
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    actor_id = uuid4()
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
        status=DecisionStatus.WAITING,
        actions=(
            ActionProposal(
                logical_action_key="initial_reply",
                action_type="send_message",
                parameters={"recipient": "customer@example.test", "body": "Hello"},
                rationale="Reply to the customer enquiry.",
            ),
        ),
        wake_conditions=(HumanWakeCondition(interaction="approval"),),
        memory_update=MemoryUpdate(
            summary="The customer is waiting for an initial response.",
            summary_source_event_ids=(event.event_id,),
            open_commitments=("Send the approved response",),
        ),
    )
    app = create_app(
        settings=_settings(environment="production"),
        session_factory=runtime_factory,
        process_registry=ProcessDefinitionRegistry([definition]),
    )

    try:
        async with admin_factory.begin() as session:
            session.add_all(
                [
                    Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="API Tenant"),
                    Tenant(
                        id=other_tenant_id,
                        slug=f"tenant-{other_tenant_id}",
                        name="Other API Tenant",
                    ),
                ]
            )
        credential_service = TenantCredentialService()
        async with admin_factory.begin() as session:
            operator_credential = await credential_service.issue(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                name="operator",
                scopes=(
                    CredentialScope.PROCESSES_READ,
                    CredentialScope.REVIEWS_READ,
                    CredentialScope.REVIEWS_COMMENT,
                    CredentialScope.REVIEWS_DECIDE,
                ),
                roles=("message_approver",),
            )
            no_role_credential = await credential_service.issue(
                session,
                tenant_id=tenant_id,
                actor_id=uuid4(),
                name="operator without approval role",
                scopes=(CredentialScope.REVIEWS_DECIDE,),
            )
            other_credential = await credential_service.issue(
                session,
                tenant_id=other_tenant_id,
                actor_id=actor_id,
                name="other tenant reader",
                scopes=(CredentialScope.PROCESSES_READ,),
            )
            control_credential = await credential_service.issue(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                name="process controller",
                scopes=(
                    CredentialScope.PROCESSES_READ,
                    CredentialScope.PROCESSES_CONTROL,
                ),
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
        process_id = ingested.process_instance_id
        turn_id = uuid4()
        async with runtime_factory.begin() as session:
            actions = await ActionGateway().persist_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                agent_turn_id=turn_id,
                process_definition_version=definition.version,
                decision=decision,
                policy=definition.action_policy(),
            )
        assert actions[0].review_thread_id is not None
        intervention_id = uuid4()
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            approval_request = await session.get(ApprovalRequest, actions[0].approval_request_id)
            assert approval_request is not None
            approval_request.required_role = "message_approver"
            action_request = await session.get(ActionRequest, actions[0].action_request_id)
            assert action_request is not None
            action_request.status = "conflict"
            conflict_attempt = ActionAttempt(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                action_request_id=actions[0].action_request_id,
                revision=1,
                attempt_number=1,
                idempotency_key="c" * 64,
                adapter_id="stub.booking.v1",
                status="conflict",
                conflict={
                    "code": "resource_unavailable",
                    "message": "the requested booking slot is no longer available",
                    "details": {"resource_type": "appointment_slot"},
                    "facts": [
                        {
                            "key": "booking.available_slots",
                            "kind": "authoritative",
                            "value": ["2026-09-04T10:00:00+00:00"],
                        }
                    ],
                },
                facts=[
                    {
                        "key": "booking.available_slots",
                        "kind": "authoritative",
                        "value": ["2026-09-04T10:00:00+00:00"],
                    }
                ],
                error="the requested booking slot is no longer available",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            session.add(conflict_attempt)
            await ProcessControlService().record_intervention(
                session,
                InterventionInput(
                    intervention_id=intervention_id,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    agent_turn_id=uuid4(),
                    kind="turn_failure",
                    error_type="DecisionRejected",
                    error="The model produced no valid progress path.",
                    event_ids=(event.event_id,),
                ),
            )
            await ProcessStateService().apply_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                agent_turn_id=turn_id,
                decision=decision,
            )

        headers = {"Authorization": f"Bearer {operator_credential.token}"}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            processes = await client.get("/v1/processes", headers=headers)
            assert processes.status_code == 200
            assert processes.json()[0]["id"] == str(process_id)
            assert processes.json()[0]["status"] == "review"
            assert processes.json()[0]["pending_reviews"] == 1
            assert processes.json()[0]["current_wake_conditions"] == [
                {"type": "human", "interaction": "approval"}
            ]

            detail = await client.get(f"/v1/processes/{process_id}", headers=headers)
            assert detail.status_code == 200
            assert detail.json()["current_wake_conditions"] == [
                {"type": "human", "interaction": "approval"}
            ]
            assert detail.json()["interventions"] == [
                {
                    "id": str(intervention_id),
                    "agent_turn_id": detail.json()["interventions"][0]["agent_turn_id"],
                    "kind": "turn_failure",
                    "status": "open",
                    "error_type": "DecisionRejected",
                    "error": "The model produced no valid progress path.",
                    "source_event_ids": [str(event.event_id)],
                    "source_review_command_ids": [],
                    "source_action_attempt_ids": [],
                    "source_timer_ids": [],
                    "resolved_by_command_id": None,
                    "resolved_at": None,
                    "created_at": detail.json()["interventions"][0]["created_at"],
                }
            ]
            assert {item["kind"] for item in detail.json()["timeline"]} >= {
                "event",
                "decision",
                "action",
            }
            decision_item = next(
                item for item in detail.json()["timeline"] if item["kind"] == "decision"
            )
            assert decision_item["detail"]["memory_summary"] == (
                "The customer is waiting for an initial response."
            )
            timeline = detail.json()["timeline"]
            decision_index = next(
                index for index, item in enumerate(timeline) if item["kind"] == "decision"
            )
            action_index = next(
                index for index, item in enumerate(timeline) if item["kind"] == "action"
            )
            assert decision_index < action_index
            assert timeline[decision_index]["agent_turn_id"] == str(turn_id)
            assert timeline[action_index]["agent_turn_id"] == str(turn_id)
            assert timeline[action_index]["action_request_id"] == str(actions[0].action_request_id)
            conflict_attempt_item = next(
                item
                for item in timeline
                if item["kind"] == "attempt" and item["status"] == "conflict"
            )
            assert conflict_attempt_item["detail"]["conflict"] == {
                "code": "resource_unavailable",
                "message": "the requested booking slot is no longer available",
                "details": {"resource_type": "appointment_slot"},
                "facts": [
                    {
                        "key": "booking.available_slots",
                        "kind": "authoritative",
                        "value": ["2026-09-04T10:00:00+00:00"],
                    }
                ],
            }
            assert conflict_attempt_item["detail"]["facts"] == [
                {
                    "key": "booking.available_slots",
                    "kind": "authoritative",
                    "value": ["2026-09-04T10:00:00+00:00"],
                }
            ]
            bounded_detail = await client.get(
                f"/v1/processes/{process_id}?timeline_limit=1", headers=headers
            )
            assert len(bounded_detail.json()["timeline"]) == 1

            reviews = await client.get("/v1/reviews", headers=headers)
            assert reviews.status_code == 200
            review = reviews.json()[0]
            assert review["thread_id"] == str(actions[0].review_thread_id)
            assert review["parameters"] == decision.actions[0].parameters

            isolated_headers = {"Authorization": f"Bearer {other_credential.token}"}
            isolated = await client.get("/v1/processes", headers=isolated_headers)
            assert isolated.json() == []
            hidden = await client.get(f"/v1/processes/{process_id}", headers=isolated_headers)
            assert hidden.status_code == 404

            command_id = uuid4()
            missing_role = await client.post(
                f"/v1/reviews/{review['thread_id']}/commands",
                headers={"Authorization": f"Bearer {no_role_credential.token}"},
                json={
                    "command_type": "approve",
                    "expected_payload_hash": review["payload_hash"],
                },
            )
            assert missing_role.status_code == 403
            assert missing_role.json()["detail"] == "approval requires role: message_approver"

            approval = await client.post(
                f"/v1/reviews/{review['thread_id']}/commands",
                headers=headers,
                json={
                    "command_id": str(command_id),
                    "command_type": "approve",
                    "message": "Approved with the warmer wording shown.",
                    "expected_payload_hash": review["payload_hash"],
                },
            )
            assert approval.status_code == 202
            assert approval.json() == {
                "command_id": str(command_id),
                "thread_status": "approved",
                "approval_status": "approved",
                "action_status": "approved",
            }
            repeated = await client.post(
                f"/v1/reviews/{review['thread_id']}/commands",
                headers=headers,
                json={
                    "command_id": str(command_id),
                    "command_type": "approve",
                    "message": "Approved with the warmer wording shown.",
                    "expected_payload_hash": review["payload_hash"],
                },
            )
            assert repeated.status_code == 202
            assert repeated.json() == approval.json()
            assert (await client.get("/v1/reviews", headers=headers)).json() == []

            forbidden_control = await client.post(
                f"/v1/processes/{process_id}/controls",
                headers=headers,
                json={"command_type": "takeover", "reason": "Operator takeover"},
            )
            assert forbidden_control.status_code == 403

            control_id = uuid4()
            control_headers = {"Authorization": f"Bearer {control_credential.token}"}
            takeover = await client.post(
                f"/v1/processes/{process_id}/controls",
                headers=control_headers,
                json={
                    "command_id": str(control_id),
                    "command_type": "takeover",
                    "reason": "Operator takeover",
                },
            )
            assert takeover.status_code == 202
            assert takeover.json() == {
                "command_id": str(control_id),
                "command_type": "takeover",
            }
            repeated_takeover = await client.post(
                f"/v1/processes/{process_id}/controls",
                headers=control_headers,
                json={
                    "command_id": str(control_id),
                    "command_type": "takeover",
                    "reason": "Operator takeover",
                },
            )
            assert repeated_takeover.status_code == 202
            assert repeated_takeover.json() == takeover.json()
            controlled_detail = await client.get(
                f"/v1/processes/{process_id}", headers=control_headers
            )
            assert controlled_detail.json()["status"] == "paused"
            assert "control" in {item["kind"] for item in controlled_detail.json()["timeline"]}
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await _delete_tenant_data(admin_factory, other_tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
