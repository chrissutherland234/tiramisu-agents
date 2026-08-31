"""Tenant-scoped dead-letter inspection and attributed requeue operations."""

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents.api.operator_auth import OperatorIdentity, require_operator_identity
from tiramisu_agents.db.models.events import OutboxMessage, OutboxRecoveryCommand
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.events.outbox_recovery import (
    OutboxRecoveryConflict,
    OutboxRecoveryService,
    RequeueOutboxInput,
)
from tiramisu_agents.security.credentials import CredentialScope

router = APIRouter(prefix="/v1/outbox", tags=["outbox-operations"])


class DeadLetterSummary(BaseModel):
    id: UUID
    process_instance_id: UUID | None
    message_type: str
    destination: str
    attempt_count: int
    last_error: str | None
    dead_lettered_at: datetime
    created_at: datetime


class RequeueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID = Field(default_factory=uuid4)
    reason: str = Field(min_length=1, max_length=10_000)


class RequeueResponse(BaseModel):
    command_id: UUID
    outbox_message_id: UUID
    status: str


class RecoveryCommandSummary(BaseModel):
    id: UUID
    outbox_message_id: UUID
    actor_id: UUID
    command_type: str
    reason: str
    previous_attempt_count: int
    previous_error: str | None
    previous_dead_lettered_at: datetime
    created_at: datetime


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


@router.get("/dead-letters", response_model=list[DeadLetterSummary])
async def list_dead_letters(
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
    process_instance_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeadLetterSummary]:
    identity.require_scope(CredentialScope.OUTBOX_READ)
    query = (
        select(OutboxMessage)
        .where(OutboxMessage.status == "dead_letter")
        .order_by(OutboxMessage.dead_lettered_at.desc(), OutboxMessage.id)
    )
    if process_instance_id is not None:
        query = query.where(OutboxMessage.process_instance_id == process_instance_id)
    async with _session_factory(request).begin() as session:
        await set_tenant_context(session, identity.tenant_id)
        messages = (await session.scalars(query.limit(limit))).all()
    return [
        DeadLetterSummary(
            id=message.id,
            process_instance_id=message.process_instance_id,
            message_type=message.message_type,
            destination=message.destination,
            attempt_count=message.attempt_count,
            last_error=message.last_error,
            dead_lettered_at=_dead_lettered_at(message),
            created_at=message.created_at,
        )
        for message in messages
    ]


@router.post(
    "/dead-letters/{outbox_message_id}/requeue",
    response_model=RequeueResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def requeue_dead_letter(
    outbox_message_id: UUID,
    body: RequeueRequest,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> RequeueResponse:
    identity.require_scope(CredentialScope.OUTBOX_REQUEUE)
    try:
        async with _session_factory(request).begin() as session:
            stored = await OutboxRecoveryService().requeue(
                session,
                RequeueOutboxInput(
                    command_id=body.command_id,
                    tenant_id=identity.tenant_id,
                    outbox_message_id=outbox_message_id,
                    actor_id=identity.actor_id,
                    reason=body.reason,
                ),
            )
    except OutboxRecoveryConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return RequeueResponse(
        command_id=stored.id,
        outbox_message_id=stored.outbox_message_id,
        status="pending",
    )


@router.get("/recovery-commands", response_model=list[RecoveryCommandSummary])
async def list_recovery_commands(
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
    outbox_message_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RecoveryCommandSummary]:
    identity.require_scope(CredentialScope.OUTBOX_READ)
    query = select(OutboxRecoveryCommand).order_by(
        OutboxRecoveryCommand.created_at.desc(), OutboxRecoveryCommand.id
    )
    if outbox_message_id is not None:
        query = query.where(OutboxRecoveryCommand.outbox_message_id == outbox_message_id)
    async with _session_factory(request).begin() as session:
        await set_tenant_context(session, identity.tenant_id)
        commands = (await session.scalars(query.limit(limit))).all()
    return [
        RecoveryCommandSummary(
            id=command.id,
            outbox_message_id=command.outbox_message_id,
            actor_id=command.actor_id,
            command_type=command.command_type,
            reason=command.reason,
            previous_attempt_count=command.previous_attempt_count,
            previous_error=command.previous_error,
            previous_dead_lettered_at=command.previous_dead_lettered_at,
            created_at=command.created_at,
        )
        for command in commands
    ]


def _dead_lettered_at(message: OutboxMessage) -> datetime:
    if message.dead_lettered_at is None:
        raise RuntimeError("dead-lettered outbox message is missing its timestamp")
    return message.dead_lettered_at
