"""Deployment tenant assignments are enforced inside every Activity boundary."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from tiramisu_agents.actions.execution import ActionExecutor
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.processes.compatibility import DeploymentCompatibility
from tiramisu_agents.temporal.activities.action_execution import (
    ActionExecutionActivities,
    ExecuteActionCommand,
)
from tiramisu_agents.temporal.activities.action_gateway import (
    ActionGatewayActivities,
    PersistActionsCommand,
)
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities, AgentTurnCommand
from tiramisu_agents.temporal.activities.process_state import (
    PersistProcessStateCommand,
    ProcessStateActivities,
    RecordProcessInterventionCommand,
)
from tiramisu_agents.testkit.deployment import TEST_DEPLOYMENT_RELEASE
from tiramisu_agents.testkit.scripted_agent import ScriptedAgent


async def _assert_not_authorized[CommandT](
    operation: Callable[[CommandT], Awaitable[object]],
    command: CommandT,
) -> None:
    with pytest.raises(ApplicationError) as raised:
        await operation(command)
    assert raised.value.type == "TenantNotAuthorized"
    assert raised.value.non_retryable is True


@pytest.mark.asyncio
async def test_all_tenant_bearing_activities_reject_unassigned_tenant_before_io() -> None:
    engine = create_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    session_factory = create_session_factory(engine)
    deployment = load_fictional_deployment()
    registry = deployment.registry
    definition = deployment.definition
    compatibility = DeploymentCompatibility(
        client_pack_fingerprint="b" * 64,
        extension_manifest_hash="a" * 64,
        definition_fingerprints={(definition.id, definition.version): definition.fingerprint()},
    )
    unauthorized_tenant = uuid4()
    authorized = frozenset({uuid4()})
    process_id = uuid4()
    turn_id = uuid4()
    agent = AgentTurnActivities(
        session_factory,
        registry,
        ScriptedAgent([]),
        compatibility=compatibility,
        deployment_release=TEST_DEPLOYMENT_RELEASE,
        authorized_tenant_ids=authorized,
    )
    gateway = ActionGatewayActivities(
        session_factory,
        registry,
        deployment_release=TEST_DEPLOYMENT_RELEASE,
        authorized_tenant_ids=authorized,
    )
    state = ProcessStateActivities(
        session_factory,
        registry,
        deployment_release=TEST_DEPLOYMENT_RELEASE,
        authorized_tenant_ids=authorized,
    )
    execution = ActionExecutionActivities(
        ActionExecutor(
            session_factory,
            ActionAdapterRegistry({}),
            compatibility,
            TEST_DEPLOYMENT_RELEASE,
            registry,
        ),
        authorized_tenant_ids=authorized,
    )

    try:
        await _assert_not_authorized(
            agent.run_agent_turn,
            AgentTurnCommand(
                tenant_id=str(unauthorized_tenant),
                process_instance_id=str(process_id),
                process_definition_id="enquiry_to_booking",
                process_definition_version=definition.version,
                turn_id=str(turn_id),
                event_ids=(),
                workflow_now=datetime.now(UTC),
            ),
        )
        await _assert_not_authorized(
            gateway.persist_agent_actions,
            PersistActionsCommand(
                tenant_id=str(unauthorized_tenant),
                process_instance_id=str(process_id),
                process_definition_id="enquiry_to_booking",
                process_definition_version=definition.version,
                agent_turn_id=str(turn_id),
                event_ids=(),
                workflow_now=datetime.now(UTC),
                decision_json="{}",
            ),
        )
        await _assert_not_authorized(
            state.persist_process_state,
            PersistProcessStateCommand(
                tenant_id=str(unauthorized_tenant),
                process_instance_id=str(process_id),
                process_definition_id="enquiry_to_booking",
                process_definition_version=definition.version,
                agent_turn_id=str(turn_id),
                event_ids=(),
                workflow_now=datetime.now(UTC),
                decision_json="{}",
            ),
        )
        await _assert_not_authorized(
            state.record_process_intervention,
            RecordProcessInterventionCommand(
                intervention_id=str(uuid4()),
                tenant_id=str(unauthorized_tenant),
                process_instance_id=str(process_id),
                agent_turn_id=str(turn_id),
                kind="turn_failure",
                error_type="TestFailure",
                error="must be rejected before database access",
            ),
        )
        await _assert_not_authorized(
            execution.execute_action,
            ExecuteActionCommand(
                tenant_id=str(unauthorized_tenant),
                process_instance_id=str(process_id),
                action_request_id=str(uuid4()),
                revision=1,
            ),
        )
    finally:
        await engine.dispose()
