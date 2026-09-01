"""Durable process interventions and attributed operator controls."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.decisions import HumanWakeCondition
from tiramisu_agents.core.reserved_events import OPERATOR_MANUAL_WAKE_EVENT_TYPE
from tiramisu_agents.db.models.events import EventInbox, OutboxMessage
from tiramisu_agents.db.models.processes import (
    ProcessControlCommand,
    ProcessInstance,
    ProcessIntervention,
)
from tiramisu_agents.db.session import set_tenant_context


class ProcessControlType(StrEnum):
    RETRY = "retry"
    WAKE = "wake"
    TAKEOVER = "takeover"
    RESUME = "resume"


class ProcessControlConflict(ValueError):
    """Raised when an operator command cannot be safely applied."""


@dataclass(frozen=True, slots=True)
class InterventionInput:
    intervention_id: UUID
    tenant_id: UUID
    process_instance_id: UUID
    agent_turn_id: UUID
    kind: str
    error_type: str
    error: str
    event_ids: tuple[UUID, ...] = ()
    review_command_ids: tuple[UUID, ...] = ()
    action_attempt_ids: tuple[UUID, ...] = ()
    timer_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessControlInput:
    command_id: UUID
    tenant_id: UUID
    process_instance_id: UUID
    actor_id: UUID
    command_type: ProcessControlType
    reason: str
    intervention_id: UUID | None = None


class ProcessControlService:
    async def record_intervention(
        self,
        session: AsyncSession,
        value: InterventionInput,
    ) -> ProcessIntervention:
        await set_tenant_context(session, value.tenant_id)
        existing = await session.scalar(
            select(ProcessIntervention).where(
                ProcessIntervention.tenant_id == value.tenant_id,
                ProcessIntervention.process_instance_id == value.process_instance_id,
                ProcessIntervention.agent_turn_id == value.agent_turn_id,
            )
        )
        if existing is not None:
            return existing
        process = await session.scalar(
            select(ProcessInstance)
            .where(
                ProcessInstance.tenant_id == value.tenant_id,
                ProcessInstance.id == value.process_instance_id,
            )
            .with_for_update()
        )
        if process is None:
            raise ProcessControlConflict("process instance not found")
        intervention = ProcessIntervention(
            id=value.intervention_id,
            tenant_id=value.tenant_id,
            process_instance_id=value.process_instance_id,
            agent_turn_id=value.agent_turn_id,
            kind=value.kind,
            status="open",
            error_type=value.error_type,
            error=value.error[:10_000],
            source_event_ids=[str(item) for item in value.event_ids],
            source_review_command_ids=[str(item) for item in value.review_command_ids],
            source_action_attempt_ids=[str(item) for item in value.action_attempt_ids],
            source_timer_ids=list(value.timer_ids),
        )
        session.add(intervention)
        if process.status not in {"paused", "completed", "cancelled", "failed"}:
            process.status = "review"
            process.current_wake_conditions = [
                HumanWakeCondition(interaction="operator").model_dump(mode="json")
            ]
        await session.flush()
        return intervention

    async def apply_control(
        self,
        session: AsyncSession,
        command: ProcessControlInput,
    ) -> ProcessControlCommand:
        await set_tenant_context(session, command.tenant_id)
        reason = command.reason.strip()
        if not reason or len(reason) > 10_000:
            raise ProcessControlConflict(
                "control command reason must contain 1 to 10000 characters"
            )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"process-control:{command.tenant_id}:{command.command_id}"},
        )
        existing = await session.scalar(
            select(ProcessControlCommand).where(
                ProcessControlCommand.tenant_id == command.tenant_id,
                ProcessControlCommand.id == command.command_id,
            )
        )
        if existing is not None:
            if (
                existing.process_instance_id != command.process_instance_id
                or existing.actor_id != command.actor_id
                or existing.command_type != command.command_type.value
                or existing.reason != reason
                or existing.intervention_id != command.intervention_id
            ):
                raise ProcessControlConflict("control command ID was reused")
            return existing
        process = await session.scalar(
            select(ProcessInstance)
            .where(
                ProcessInstance.tenant_id == command.tenant_id,
                ProcessInstance.id == command.process_instance_id,
            )
            .with_for_update()
        )
        if process is None:
            raise ProcessControlConflict("process instance not found")
        if process.status in {"completed", "cancelled", "failed"}:
            raise ProcessControlConflict("terminal processes cannot be controlled")

        intervention: ProcessIntervention | None = None
        payload: dict[str, object] = {}
        if command.command_type is ProcessControlType.RETRY:
            if command.intervention_id is None:
                raise ProcessControlConflict("retry requires an intervention ID")
            intervention = await session.scalar(
                select(ProcessIntervention)
                .where(
                    ProcessIntervention.tenant_id == command.tenant_id,
                    ProcessIntervention.process_instance_id == command.process_instance_id,
                    ProcessIntervention.id == command.intervention_id,
                )
                .with_for_update()
            )
            if intervention is None or intervention.status != "open":
                raise ProcessControlConflict("intervention is not open")
            payload = {
                "event_ids": intervention.source_event_ids,
                "review_command_ids": intervention.source_review_command_ids,
                "action_attempt_ids": intervention.source_action_attempt_ids,
                "timer_ids": intervention.source_timer_ids,
            }
            process.status = "active"
            process.current_wake_conditions = []
        elif command.command_type is ProcessControlType.TAKEOVER:
            if process.status == "paused":
                raise ProcessControlConflict("process is already paused")
            process.status = "paused"
            process.current_wake_conditions = [
                HumanWakeCondition(interaction="operator").model_dump(mode="json")
            ]
        elif command.command_type is ProcessControlType.RESUME:
            if process.status != "paused":
                raise ProcessControlConflict("only a paused process can be resumed")
            process.status = "active"
            process.current_wake_conditions = []
        elif command.command_type is ProcessControlType.WAKE:
            if process.status == "paused":
                raise ProcessControlConflict("resume a paused process before waking it")

        stored = ProcessControlCommand(
            id=command.command_id,
            tenant_id=command.tenant_id,
            process_instance_id=command.process_instance_id,
            actor_id=command.actor_id,
            intervention_id=command.intervention_id,
            command_type=command.command_type.value,
            reason=reason,
            payload=payload,
        )
        session.add(stored)
        if intervention is not None:
            intervention.status = "resolved"
            intervention.resolved_by_command_id = command.command_id
            intervention.resolved_at = datetime.now(UTC)

        if command.command_type in {ProcessControlType.WAKE, ProcessControlType.RESUME}:
            await self._enqueue_manual_wake(session, process, command)
        else:
            await self._enqueue_control(session, process, command, payload)
        await session.flush()
        return stored

    @staticmethod
    async def _enqueue_manual_wake(
        session: AsyncSession,
        process: ProcessInstance,
        command: ProcessControlInput,
    ) -> None:
        event_id = uuid4()
        recorded_at = datetime.now(UTC)
        session.add(
            EventInbox(
                id=event_id,
                tenant_id=command.tenant_id,
                process_instance_id=command.process_instance_id,
                source="operator",
                source_event_id=str(command.command_id),
                event_type=OPERATOR_MANUAL_WAKE_EVENT_TYPE,
                event_data={
                    "event_id": str(event_id),
                    "tenant_id": str(command.tenant_id),
                    "process_instance_id": str(command.process_instance_id),
                    "source": "operator",
                    "source_event_id": str(command.command_id),
                    "event_type": OPERATOR_MANUAL_WAKE_EVENT_TYPE,
                    "occurred_at": recorded_at.isoformat(),
                    "received_at": recorded_at.isoformat(),
                    "payload": {
                        "reason": command.reason,
                        "actor_id": str(command.actor_id),
                        "command_type": command.command_type.value,
                    },
                    "facts": [],
                    "external_references": [],
                },
                correlation_status="matched",
                correlation_reason="operator_manual_wake",
                received_at=recorded_at,
            )
        )
        await session.execute(
            insert(OutboxMessage)
            .values(
                id=uuid4(),
                tenant_id=command.tenant_id,
                process_instance_id=command.process_instance_id,
                causation_event_id=event_id,
                message_type="temporal.process_event",
                destination=process.workflow_id,
                deduplication_key=f"process-control:{command.command_id}",
                payload={
                    "event_id": str(event_id),
                    "event_type": OPERATOR_MANUAL_WAKE_EVENT_TYPE,
                },
            )
            .on_conflict_do_nothing(constraint="uq_outbox_messages_dedup")
        )

    @staticmethod
    async def _enqueue_control(
        session: AsyncSession,
        process: ProcessInstance,
        command: ProcessControlInput,
        payload: dict[str, object],
    ) -> None:
        await session.execute(
            insert(OutboxMessage)
            .values(
                id=uuid4(),
                tenant_id=command.tenant_id,
                process_instance_id=command.process_instance_id,
                message_type="temporal.process_control",
                destination=process.workflow_id,
                deduplication_key=f"process-control:{command.command_id}",
                payload={
                    "command_id": str(command.command_id),
                    "command_type": command.command_type.value,
                    **payload,
                },
            )
            .on_conflict_do_nothing(constraint="uq_outbox_messages_dedup")
        )
