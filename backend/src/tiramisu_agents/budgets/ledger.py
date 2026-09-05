"""PostgreSQL projection for durable model token/cost budgets."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.budgets.policy import (
    ModelBudget,
    ModelBudgetSnapshot,
    ModelUsage,
    evaluate_model_budget,
)
from tiramisu_agents.db.models.usage import ModelUsageLedger
from tiramisu_agents.db.session import set_tenant_context

_DEFAULT_EXECUTION_ID = UUID(int=0)


class ModelUsageService:
    """Append recorded model spend; the ledger survives retries and rollovers."""

    async def record(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        agent_turn_id: UUID,
        attempt_number: int,
        model: str,
        usage: ModelUsage,
        cost_micros: int,
        price_table_version: int,
        execution_id: UUID = _DEFAULT_EXECUTION_ID,
    ) -> UUID:
        if type(attempt_number) is not int or attempt_number < 1:
            raise ValueError("usage attempt number must be a positive integer")
        if not model.strip():
            raise ValueError("a model name is required to record usage")
        if type(cost_micros) is not int or cost_micros < 0:
            raise ValueError("recorded model cost must be a non-negative integer of micro-USD")
        if type(price_table_version) is not int or price_table_version < 1:
            raise ValueError("recorded price-table version must be a positive integer")
        await set_tenant_context(session, tenant_id)
        inserted_id = await session.scalar(
            insert(ModelUsageLedger)
            .values(
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                agent_turn_id=agent_turn_id,
                execution_id=execution_id,
                attempt_number=attempt_number,
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_micros=cost_micros,
                price_table_version=price_table_version,
            )
            .on_conflict_do_nothing(constraint="uq_model_usage_turn_attempt")
            .returning(ModelUsageLedger.id)
        )
        if inserted_id is not None:
            return inserted_id
        existing = await session.scalar(
            select(ModelUsageLedger).where(
                ModelUsageLedger.tenant_id == tenant_id,
                ModelUsageLedger.process_instance_id == process_instance_id,
                ModelUsageLedger.agent_turn_id == agent_turn_id,
                ModelUsageLedger.execution_id == execution_id,
                ModelUsageLedger.attempt_number == attempt_number,
            )
        )
        if existing is None:
            raise ValueError("model usage identity could not be reserved")
        if (
            existing.model != model
            or existing.input_tokens != usage.input_tokens
            or existing.output_tokens != usage.output_tokens
            or existing.cost_micros != cost_micros
            or existing.price_table_version != price_table_version
        ):
            raise ValueError("model usage lineage changed during replay")
        return existing.id

    async def spent(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
    ) -> tuple[ModelUsage, int]:
        await set_tenant_context(session, tenant_id)
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(ModelUsageLedger.input_tokens), 0),
                    func.coalesce(func.sum(ModelUsageLedger.output_tokens), 0),
                    func.coalesce(func.sum(ModelUsageLedger.cost_micros), 0),
                ).where(
                    ModelUsageLedger.tenant_id == tenant_id,
                    ModelUsageLedger.process_instance_id == process_instance_id,
                )
            )
        ).one()
        return ModelUsage(input_tokens=int(row[0]), output_tokens=int(row[1])), int(row[2])

    async def inspect(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        budget: ModelBudget,
    ) -> ModelBudgetSnapshot:
        usage, cost_micros = await self.spent(
            session, tenant_id=tenant_id, process_instance_id=process_instance_id
        )
        return evaluate_model_budget(budget=budget, spent=usage, spent_cost_micros=cost_micros)

    async def tenant_spent(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
    ) -> tuple[ModelUsage, int]:
        await set_tenant_context(session, tenant_id)
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(ModelUsageLedger.input_tokens), 0),
                    func.coalesce(func.sum(ModelUsageLedger.output_tokens), 0),
                    func.coalesce(func.sum(ModelUsageLedger.cost_micros), 0),
                ).where(ModelUsageLedger.tenant_id == tenant_id)
            )
        ).one()
        return ModelUsage(input_tokens=int(row[0]), output_tokens=int(row[1])), int(row[2])
