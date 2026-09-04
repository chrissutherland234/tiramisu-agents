"""The public executable scenario running through PostgreSQL and Temporal."""

import os
from datetime import timedelta

import pytest
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.testkit import (
    PostgresTemporalScenarioDriver,
    ScenarioRunError,
    ScenarioTraceKind,
    run_scenario,
)
from tiramisu_agents.testkit.example_projects import create_timer_project
from tiramisu_agents.testkit.temporal_environment import start_time_skipping_environment

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires migrated PostgreSQL and Temporal's test server",
)


@pytest.mark.asyncio
async def test_happy_path_uses_one_script_in_the_kernel_and_durable_runtime() -> None:
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

    try:
        kernel_result = await run_scenario(load_fictional_deployment(), "happy_path")
        durable_pack = load_fictional_deployment()
        production_adapters = {id(adapter): adapter for adapter in durable_pack.bindings.values()}

        async with await start_time_skipping_environment() as environment:
            driver = PostgresTemporalScenarioDriver(
                durable_pack,
                session_factory=runtime_factory,
                admin_session_factory=admin_factory,
                environment=environment,
            )
            durable_result = await driver.run("happy_path")

        assert durable_result.passed is True
        assert durable_result.action_types == kernel_result.action_types
        assert durable_result.approval_count == kernel_result.approval_count == 3
        assert durable_result.authoritative_facts["booking.status"] == "confirmed"
        assert durable_result.authoritative_facts["payment.status"] == "completed"
        assert durable_result.authoritative_facts["calendar.status"] == "created"
        assert driver.worker_start_count >= 5
        assert all(
            getattr(adapter, "requests", []) == [] for adapter in production_adapters.values()
        )
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_timer_scenario_uses_temporal_time_skipping() -> None:
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

    try:
        kernel_result = await run_scenario(create_timer_project().compile(), "follow_up")
        durable_pack = create_timer_project().compile()
        async with await start_time_skipping_environment() as environment:
            driver = PostgresTemporalScenarioDriver(
                durable_pack,
                session_factory=runtime_factory,
                admin_session_factory=admin_factory,
                environment=environment,
            )
            durable_result = await driver.run("follow_up")

        wake = next(entry for entry in durable_result.trace if entry.kind is ScenarioTraceKind.WAKE)
        action = next(
            entry for entry in durable_result.trace if entry.kind is ScenarioTraceKind.ACTION
        )
        assert durable_result.action_types == kernel_result.action_types == ("finish_work",)
        assert durable_result.authoritative_facts == {"work.status": "completed"}
        assert action.occurred_at - wake.occurred_at >= timedelta(hours=1)
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_durable_scenario_failure_names_the_exact_compiled_step() -> None:
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

    try:
        durable_pack = create_timer_project(expected_status="open").compile()
        async with await start_time_skipping_environment() as environment:
            driver = PostgresTemporalScenarioDriver(
                durable_pack,
                session_factory=runtime_factory,
                admin_session_factory=admin_factory,
                environment=environment,
            )
            with pytest.raises(
                ScenarioRunError,
                match=(
                    "scenario follow_up step 'The provider result is authoritative.': "
                    "fact work.status is 'completed'; expected 'open'"
                ),
            ):
                await driver.run("follow_up")
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()
