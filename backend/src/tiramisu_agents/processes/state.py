"""Idempotent persistence of process knowledge, memory, and lifecycle state."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.actions import ActionRequestStatus
from tiramisu_agents.core.contracts.decisions import (
    AgentDecision,
    WakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.knowledge import FactObservation
from tiramisu_agents.core.contracts.processes import ProcessStatus
from tiramisu_agents.core.limits import (
    require_memory_content,
    require_process_fact_projection,
)
from tiramisu_agents.core.transitions import (
    ProcessTransitionRejected,
    apply_fact_observations,
    project_process_transition,
)
from tiramisu_agents.db.models.actions import ActionAttempt, ActionRequest
from tiramisu_agents.db.models.events import EventInbox
from tiramisu_agents.db.models.processes import ProcessInstance, ProcessStateRevision
from tiramisu_agents.db.session import set_tenant_context


class ProcessStateConflict(ValueError):
    """Raised when a turn cannot safely update the current process projection."""


_wake_condition_adapter: TypeAdapter[WakeCondition] = TypeAdapter(WakeCondition)


@dataclass(frozen=True, slots=True)
class AppliedProcessState:
    process_instance_id: UUID
    agent_turn_id: UUID
    decision_id: UUID
    version: int
    status: ProcessStatus
    wake_conditions: tuple[WakeCondition, ...]
    pending_action_request_ids: tuple[UUID, ...]
    terminal: bool


class ProcessStateService:
    """Project validated turn inputs and decisions into durable process state."""

    async def apply_decision(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        agent_turn_id: UUID,
        decision: AgentDecision,
        terminal_states: frozenset[ProcessStatus] | None = None,
        completion_requirements: dict[str, Any] | None = None,
    ) -> AppliedProcessState:
        await set_tenant_context(session, tenant_id)
        process = await session.scalar(
            select(ProcessInstance)
            .where(
                ProcessInstance.tenant_id == tenant_id,
                ProcessInstance.id == process_instance_id,
            )
            .with_for_update()
        )
        if process is None:
            raise ProcessStateConflict("process instance not found")

        existing = await session.scalar(
            select(ProcessStateRevision).where(
                ProcessStateRevision.tenant_id == tenant_id,
                ProcessStateRevision.process_instance_id == process_instance_id,
                ProcessStateRevision.agent_turn_id == agent_turn_id,
            )
        )
        if existing is not None:
            if existing.decision_id != decision.decision_id:
                raise ProcessStateConflict("agent turn was reused for another decision")
            open_actions = await self._load_open_actions(
                session,
                process_instance_id=process_instance_id,
            )
            return self._result(existing, open_actions=open_actions)
        reused_decision = await session.scalar(
            select(ProcessStateRevision.id).where(
                ProcessStateRevision.tenant_id == tenant_id,
                ProcessStateRevision.process_instance_id == process_instance_id,
                ProcessStateRevision.decision_id == decision.decision_id,
            )
        )
        if reused_decision is not None:
            raise ProcessStateConflict("decision was reused for another agent turn")
        if process.status in {
            ProcessStatus.COMPLETED.value,
            ProcessStatus.CANCELLED.value,
            ProcessStatus.FAILED.value,
        }:
            raise ProcessStateConflict("terminal process state cannot accept another decision")
        if process.status == ProcessStatus.PAUSED.value:
            raise ProcessStateConflict("paused process cannot accept an agent decision")

        events = await self._load_events(
            session,
            process_instance_id=process_instance_id,
            event_ids=decision.based_on_event_ids,
        )
        attempts = await self._load_attempts(
            session,
            process_instance_id=process_instance_id,
            attempt_ids=decision.based_on_action_attempt_ids,
        )
        authoritative = dict(process.authoritative_facts)
        claims = dict(process.customer_claims)
        provenance = dict(process.fact_provenance)
        for event_id in decision.based_on_event_ids:
            event = events[event_id]
            apply_fact_observations(
                event.facts,
                source_type="event",
                source_id=event_id,
                authoritative=authoritative,
                claims=claims,
                provenance=provenance,
            )
        for attempt_id in decision.based_on_action_attempt_ids:
            attempt = attempts[attempt_id]
            apply_fact_observations(
                tuple(FactObservation.model_validate(fact) for fact in attempt.facts),
                source_type="action_attempt",
                source_id=attempt_id,
                authoritative=authoritative,
                claims=claims,
                provenance=provenance,
            )
        try:
            require_process_fact_projection(
                authoritative_facts=authoritative,
                customer_claims=claims,
                fact_provenance=provenance,
            )
        except ValueError as error:
            raise ProcessStateConflict(str(error)) from error

        open_actions = await self._load_open_actions(
            session,
            process_instance_id=process_instance_id,
        )
        try:
            transition = project_process_transition(
                decision=decision,
                open_actions=open_actions,
                terminal_states=terminal_states,
                authoritative_facts=authoritative,
                completion_requirements=completion_requirements or {},
            )
        except ProcessTransitionRejected as error:
            raise ProcessStateConflict(str(error)) from error
        status = transition.status
        memory = decision.memory_update
        try:
            require_memory_content(
                summary=memory.summary,
                open_commitments=memory.open_commitments,
            )
        except ValueError as error:
            raise ProcessStateConflict(str(error)) from error
        if memory.summary is not None:
            process.memory_summary = memory.summary
            process.memory_summary_source_event_ids = [
                str(value) for value in memory.summary_source_event_ids
            ]
            process.memory_summary_source_review_command_ids = [
                str(value) for value in memory.summary_source_review_command_ids
            ]
            process.memory_summary_source_action_attempt_ids = [
                str(value) for value in memory.summary_source_action_attempt_ids
            ]
            process.memory_summary_source_timer_ids = list(memory.summary_source_timer_ids)
        process.open_commitments = list(memory.open_commitments)
        wake_conditions = transition.wake_conditions
        process.current_wake_conditions = [wake.model_dump(mode="json") for wake in wake_conditions]
        process.authoritative_facts = authoritative
        process.customer_claims = claims
        process.fact_provenance = provenance
        process.status = status.value
        process.state_version += 1

        revision = ProcessStateRevision(
            tenant_id=tenant_id,
            process_instance_id=process_instance_id,
            agent_turn_id=agent_turn_id,
            decision_id=decision.decision_id,
            version=process.state_version,
            decision_status=decision.status.value,
            process_status=status.value,
            authoritative_facts=authoritative,
            customer_claims=claims,
            fact_provenance=provenance,
            memory_summary=process.memory_summary,
            memory_summary_source_event_ids=list(process.memory_summary_source_event_ids),
            memory_summary_source_review_command_ids=list(
                process.memory_summary_source_review_command_ids
            ),
            memory_summary_source_action_attempt_ids=list(
                process.memory_summary_source_action_attempt_ids
            ),
            memory_summary_source_timer_ids=list(process.memory_summary_source_timer_ids),
            open_commitments=list(process.open_commitments),
            wake_conditions=list(process.current_wake_conditions),
            based_on_event_ids=[str(value) for value in decision.based_on_event_ids],
            based_on_review_command_ids=[
                str(value) for value in decision.based_on_review_command_ids
            ],
            based_on_action_attempt_ids=[
                str(value) for value in decision.based_on_action_attempt_ids
            ],
            based_on_timer_ids=list(decision.based_on_timer_ids),
        )
        session.add(revision)
        await session.flush()
        return self._result(revision, open_actions=open_actions)

    @staticmethod
    async def _load_events(
        session: AsyncSession,
        *,
        process_instance_id: UUID,
        event_ids: tuple[UUID, ...],
    ) -> dict[UUID, CanonicalEvent]:
        stored = (
            await session.scalars(
                select(EventInbox).where(
                    EventInbox.id.in_(event_ids),
                    EventInbox.process_instance_id == process_instance_id,
                    EventInbox.correlation_status == "matched",
                )
            )
        ).all()
        events = {
            item.id: CanonicalEvent.model_validate(item.event_data).model_copy(
                update={"process_instance_id": process_instance_id}
            )
            for item in stored
        }
        if set(events) != set(event_ids):
            raise ProcessStateConflict("one or more decision events are unavailable")
        return events

    @staticmethod
    async def _load_attempts(
        session: AsyncSession,
        *,
        process_instance_id: UUID,
        attempt_ids: tuple[UUID, ...],
    ) -> dict[UUID, ActionAttempt]:
        stored = (
            await session.scalars(
                select(ActionAttempt).where(
                    ActionAttempt.id.in_(attempt_ids),
                    ActionAttempt.process_instance_id == process_instance_id,
                )
            )
        ).all()
        attempts = {item.id: item for item in stored}
        if set(attempts) != set(attempt_ids):
            raise ProcessStateConflict("one or more decision action attempts are unavailable")
        return attempts

    @staticmethod
    async def _load_open_actions(
        session: AsyncSession,
        *,
        process_instance_id: UUID,
    ) -> tuple[tuple[UUID, ActionRequestStatus], ...]:
        rows = (
            await session.execute(
                select(ActionRequest.id, ActionRequest.status).where(
                    ActionRequest.process_instance_id == process_instance_id,
                    ActionRequest.status.in_(
                        (
                            ActionRequestStatus.ALLOWED.value,
                            ActionRequestStatus.PENDING_APPROVAL.value,
                            ActionRequestStatus.APPROVED.value,
                            ActionRequestStatus.EXECUTING.value,
                            ActionRequestStatus.UNKNOWN.value,
                            ActionRequestStatus.RECONCILING.value,
                        )
                    ),
                )
            )
        ).all()
        return tuple((row.id, ActionRequestStatus(row.status)) for row in rows)

    @staticmethod
    def _result(
        revision: ProcessStateRevision,
        *,
        open_actions: tuple[tuple[UUID, ActionRequestStatus], ...],
    ) -> AppliedProcessState:
        wake_conditions = tuple(
            _wake_condition_adapter.validate_python(value) for value in revision.wake_conditions
        )
        return AppliedProcessState(
            process_instance_id=revision.process_instance_id,
            agent_turn_id=revision.agent_turn_id,
            decision_id=revision.decision_id,
            version=revision.version,
            status=ProcessStatus(revision.process_status),
            wake_conditions=wake_conditions,
            pending_action_request_ids=tuple(action_id for action_id, _ in open_actions),
            terminal=revision.process_status
            in {
                ProcessStatus.COMPLETED.value,
                ProcessStatus.CANCELLED.value,
                ProcessStatus.FAILED.value,
            },
        )
