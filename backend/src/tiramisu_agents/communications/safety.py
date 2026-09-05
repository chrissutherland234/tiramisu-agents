"""PostgreSQL projection for deterministic customer-communication policy."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.communications.policy import (
    CommunicationBlock,
    CommunicationPolicy,
    CommunicationSafetyFacts,
    CommunicationSafetySnapshot,
    evaluate_communication_safety,
    evaluate_process_lifetime,
)
from tiramisu_agents.core.contracts.actions import ActionRequestStatus
from tiramisu_agents.db.models.actions import ActionRequest
from tiramisu_agents.db.models.events import EventInbox
from tiramisu_agents.db.models.processes import ProcessInstance

_RESERVED_OUTBOUND_STATUSES = (
    ActionRequestStatus.ALLOWED.value,
    ActionRequestStatus.PENDING_APPROVAL.value,
    ActionRequestStatus.APPROVED.value,
    ActionRequestStatus.EXECUTING.value,
    ActionRequestStatus.SUCCEEDED.value,
    ActionRequestStatus.UNKNOWN.value,
    ActionRequestStatus.RECONCILING.value,
)


class CommunicationSafetyService:
    """Project the next-send budget from immutable events and durable action records."""

    async def inspect(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process: ProcessInstance,
        policy: CommunicationPolicy,
        now: datetime,
        current_action_request_id: UUID | None = None,
    ) -> CommunicationSafetySnapshot:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("communication policy evaluation time must be timezone-aware")
        now = now.astimezone(UTC)

        action_rows = (
            await session.execute(
                select(ActionRequest.id, ActionRequest.created_at)
                .where(
                    ActionRequest.tenant_id == tenant_id,
                    ActionRequest.process_instance_id == process.id,
                    ActionRequest.action_type.in_(policy.outbound_action_types),
                    ActionRequest.status.in_(_RESERVED_OUTBOUND_STATUSES),
                )
                .order_by(ActionRequest.created_at.desc(), ActionRequest.id)
            )
        ).all()
        current_is_reserved = current_action_request_id is not None and any(
            row.id == current_action_request_id for row in action_rows
        )
        if current_action_request_id is not None and not current_is_reserved:
            raise ValueError("current outbound action is not a durable communication reservation")
        reservation = 0 if current_action_request_id is not None else 1

        last_human_reply_at = await self._latest_event_time(
            session,
            tenant_id=tenant_id,
            process_instance_id=process.id,
            event_types=policy.reply_event_types,
        )
        opted_out_at = await self._latest_event_time(
            session,
            tenant_id=tenant_id,
            process_instance_id=process.id,
            event_types=policy.opt_out_event_types,
        )
        latest_automated_response_at = await self._active_automated_response_time(
            session,
            tenant_id=tenant_id,
            process_instance_id=process.id,
            reply_event_types=policy.reply_event_types,
            automated_response_event_types=policy.automated_response_event_types,
        )

        return evaluate_communication_safety(
            policy=policy,
            facts=CommunicationSafetyFacts(
                process_created_at=process.created_at,
                outbound_message_times=tuple(row.created_at for row in action_rows),
                prior_outbound_message_times=tuple(
                    row.created_at for row in action_rows if row.id != current_action_request_id
                ),
                last_human_reply_at=last_human_reply_at,
                latest_automated_response_at=latest_automated_response_at,
                opted_out_at=opted_out_at,
                reserve_next_message=bool(reservation),
            ),
            now=now,
        )

    @staticmethod
    def process_lifetime_block(
        *,
        process: ProcessInstance,
        policy: CommunicationPolicy,
        now: datetime,
    ) -> CommunicationBlock | None:
        return evaluate_process_lifetime(
            process_created_at=process.created_at,
            policy=policy,
            now=now,
        )

    @staticmethod
    async def _latest_event_time(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        event_types: frozenset[str],
    ) -> datetime | None:
        if not event_types:
            return None
        return await session.scalar(
            select(EventInbox.received_at)
            .where(
                EventInbox.tenant_id == tenant_id,
                EventInbox.process_instance_id == process_instance_id,
                EventInbox.correlation_status == "matched",
                EventInbox.event_type.in_(event_types),
            )
            .order_by(EventInbox.received_at.desc(), EventInbox.id.desc())
            .limit(1)
        )

    @staticmethod
    async def _active_automated_response_time(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        reply_event_types: frozenset[str],
        automated_response_event_types: frozenset[str],
    ) -> datetime | None:
        if not automated_response_event_types:
            return None
        relevant_types = reply_event_types | automated_response_event_types
        latest = (
            await session.execute(
                select(EventInbox.event_type, EventInbox.received_at)
                .where(
                    EventInbox.tenant_id == tenant_id,
                    EventInbox.process_instance_id == process_instance_id,
                    EventInbox.correlation_status == "matched",
                    EventInbox.event_type.in_(relevant_types),
                )
                .order_by(EventInbox.received_at.desc(), EventInbox.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if latest is None or latest.event_type not in automated_response_event_types:
            return None
        return latest.received_at
