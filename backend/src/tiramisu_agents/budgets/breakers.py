"""Tenant circuit breakers: manual audited trips plus spend-based auto-trips."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.budgets.policy import (
    ModelBudgetBlock,
    ModelBudgetBlockCode,
    ModelUsage,
)
from tiramisu_agents.db.models.breakers import CircuitBreaker
from tiramisu_agents.db.session import set_tenant_context


class BreakerScope(StrEnum):
    MODEL_CALLS = "model_calls"
    OUTBOUND_MESSAGES = "outbound_messages"
    CAPABILITY = "capability"
    ALL = "all"


class BreakerConflict(ValueError):
    """Raised when tripping an open breaker or resetting a closed one."""


@dataclass(frozen=True, slots=True)
class BreakerState:
    scope: BreakerScope
    target: str
    tripped: bool
    reason: str
    actor_id: UUID
    transitioned_at: datetime


@dataclass(frozen=True, slots=True)
class TenantSpendCaps:
    max_tokens: int
    max_cost_micros: int


DEFAULT_TENANT_SPEND_CAPS = TenantSpendCaps(
    max_tokens=100_000_000,
    max_cost_micros=2_000_000_000,
)


def normalize_breaker_target(scope: BreakerScope, target: str) -> str:
    """Coerce breaker targets to the canonical form enforced by the schema."""

    if scope is BreakerScope.CAPABILITY:
        if not target.strip():
            raise ValueError("capability breakers require a non-blank action type")
        if len(target.strip()) > 200:
            raise ValueError("capability breaker action type is too long")
        return target.strip()
    if target != "":
        raise ValueError("only capability breakers accept a target")
    return ""


def evaluate_tenant_spend(
    *,
    spent: ModelUsage,
    spent_cost_micros: int,
    caps: TenantSpendCaps,
) -> ModelBudgetBlock | None:
    """Return the exact tenant spend block, if the tenant fence is breached."""

    if spent.total_tokens >= caps.max_tokens:
        return ModelBudgetBlock(
            ModelBudgetBlockCode.TENANT_SPEND_LIMIT,
            "tenant model token spend has reached its platform cap",
        )
    if spent_cost_micros >= caps.max_cost_micros:
        return ModelBudgetBlock(
            ModelBudgetBlockCode.TENANT_SPEND_LIMIT,
            "tenant model cost spend has reached its platform cap",
        )
    return None


class CircuitBreakerService:
    """Append manual breaker transitions; the latest row per scope/target wins."""

    async def latest(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        scope: BreakerScope,
        target: str = "",
    ) -> BreakerState | None:
        await set_tenant_context(session, tenant_id)
        row = await session.scalar(
            select(CircuitBreaker)
            .where(
                CircuitBreaker.tenant_id == tenant_id,
                CircuitBreaker.scope == scope.value,
                CircuitBreaker.target == normalize_breaker_target(scope, target),
            )
            .order_by(desc(CircuitBreaker.created_at), desc(CircuitBreaker.id))
            .limit(1)
        )
        if row is None:
            return None
        return BreakerState(
            scope=scope,
            target=row.target,
            tripped=row.tripped,
            reason=row.reason,
            actor_id=row.actor_id,
            transitioned_at=row.created_at,
        )

    async def trip(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        scope: BreakerScope,
        target: str,
        actor_id: UUID,
        reason: str,
    ) -> BreakerState:
        return await self._transition(
            session,
            tenant_id=tenant_id,
            scope=scope,
            target=target,
            actor_id=actor_id,
            reason=reason,
            tripped=True,
        )

    async def reset(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        scope: BreakerScope,
        target: str,
        actor_id: UUID,
        reason: str,
    ) -> BreakerState:
        return await self._transition(
            session,
            tenant_id=tenant_id,
            scope=scope,
            target=target,
            actor_id=actor_id,
            reason=reason,
            tripped=False,
        )

    async def open_for_model_calls(
        self, session: AsyncSession, *, tenant_id: UUID
    ) -> BreakerState | None:
        for scope in (BreakerScope.ALL, BreakerScope.MODEL_CALLS):
            state = await self.latest(session, tenant_id=tenant_id, scope=scope)
            if state is not None and state.tripped:
                return state
        return None

    async def open_for_outbound(
        self, session: AsyncSession, *, tenant_id: UUID
    ) -> BreakerState | None:
        for scope in (BreakerScope.ALL, BreakerScope.OUTBOUND_MESSAGES):
            state = await self.latest(session, tenant_id=tenant_id, scope=scope)
            if state is not None and state.tripped:
                return state
        return None

    async def open_for_capability(
        self, session: AsyncSession, *, tenant_id: UUID, action_type: str
    ) -> BreakerState | None:
        state = await self.latest(session, tenant_id=tenant_id, scope=BreakerScope.ALL)
        if state is not None and state.tripped:
            return state
        state = await self.latest(
            session, tenant_id=tenant_id, scope=BreakerScope.CAPABILITY, target=action_type
        )
        return state if state is not None and state.tripped else None

    async def _transition(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        scope: BreakerScope,
        target: str,
        actor_id: UUID,
        reason: str,
        tripped: bool,
    ) -> BreakerState:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("a breaker transition requires a reason")
        if len(normalized_reason) > 10_000:
            raise ValueError("breaker transition reason is too long")
        normalized_target = normalize_breaker_target(scope, target)
        await set_tenant_context(session, tenant_id)
        # Lock the identity even before its first transition exists. A row lock
        # on the append-only history cannot serialize concurrent initial trips.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"breaker:{tenant_id}:{scope.value}:{normalized_target}", 0
                    )
                )
            )
        )
        current = await self.latest(session, tenant_id=tenant_id, scope=scope, target=target)
        if (current.tripped if current is not None else False) == tripped:
            state = "tripped" if tripped else "closed"
            raise BreakerConflict(f"breaker is already {state}")
        transitioned_at = datetime.now(UTC)
        if current is not None:
            transitioned_at = max(
                transitioned_at, current.transitioned_at + timedelta(microseconds=1)
            )
        row = CircuitBreaker(
            tenant_id=tenant_id,
            scope=scope.value,
            target=normalized_target,
            tripped=tripped,
            reason=normalized_reason,
            actor_id=actor_id,
            created_at=transitioned_at,
        )
        session.add(row)
        await session.flush()
        return BreakerState(
            scope=scope,
            target=normalized_target,
            tripped=tripped,
            reason=normalized_reason,
            actor_id=actor_id,
            transitioned_at=row.created_at,
        )
