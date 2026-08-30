from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from agents import Agent
from agents.agent_output import AgentOutputSchema
from tiramisu_agents.agents.openai_runner import (
    ActionProposalOutput,
    AgentDecisionOutput,
    AgentsSDKExecutor,
    OpenAIAgentsTurnRunner,
)
from tiramisu_agents.core.contracts.actions import ActionAttemptStatus
from tiramisu_agents.core.contracts.decisions import DecisionStatus
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.processes import (
    ActionResultContext,
    AgentTurnInput,
    ProcessSnapshot,
    ProcessStatus,
)


@pytest.mark.asyncio
async def test_openai_runner_is_structured_proposal_only_and_bounded() -> None:
    event_id = uuid4()
    attempt_id = uuid4()
    output = AgentDecisionOutput(
        based_on_event_ids=(str(event_id),),
        based_on_action_attempt_ids=(str(attempt_id),),
        status=DecisionStatus.COMPLETED,
        actions=(
            ActionProposalOutput(
                logical_action_key="send_confirmation",
                action_type="send_message",
                parameters_json='{"template":"confirmed"}',
                rationale="The booking is confirmed.",
            ),
        ),
    )
    captured: dict[str, object] = {}

    async def execute(agent: Any, prompt: str, max_turns: int, run_config: Any) -> Any:
        captured.update(
            agent=agent,
            prompt=prompt,
            max_turns=max_turns,
            run_config=run_config,
        )
        return SimpleNamespace(final_output=output)

    tenant_id = uuid4()
    process_id = uuid4()
    runner = OpenAIAgentsTurnRunner(
        model="test-model",
        executor=cast(AgentsSDKExecutor, execute),
    )
    result = await runner.run_turn(
        AgentTurnInput(
            turn_id=uuid4(),
            process=ProcessSnapshot(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                process_type="test_process",
                process_definition_version="1",
                status=ProcessStatus.ACTIVE,
            ),
            events=(
                CanonicalEvent(
                    event_id=event_id,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    event_type="test.started",
                    source="test",
                    source_event_id="source-1",
                    occurred_at=datetime.now(UTC),
                ),
            ),
            action_results=(
                ActionResultContext(
                    attempt_id=attempt_id,
                    action_request_id=uuid4(),
                    revision=1,
                    action_type="send_message",
                    parameters={"template": "confirmed"},
                    status=ActionAttemptStatus.SUCCEEDED,
                    adapter_id="stub.messaging.v1",
                    idempotency_key="a" * 64,
                    provider_reference="message-123",
                    result={"sent": True},
                    operator_resolution_id=uuid4(),
                    operator_actor_id=uuid4(),
                    operator_evidence="Provider support confirmed delivery.",
                ),
            ),
            instructions="Only complete this fictional process.",
        )
    )

    assert result.based_on_event_ids == (event_id,)
    assert result.based_on_action_attempt_ids == (attempt_id,)
    assert result.actions[0].parameters == {"template": "confirmed"}
    assert captured["max_turns"] == 1
    agent = cast(Agent[Any], captured["agent"])
    assert agent.tools == []
    assert agent.handoffs == []
    assert agent.output_type is AgentDecisionOutput
    assert "source-1" in cast(str, captured["prompt"])
    assert "message-123" in cast(str, captured["prompt"])
    assert "Provider support confirmed delivery." in cast(str, captured["prompt"])


def test_openai_transport_is_a_strict_sdk_output_schema() -> None:
    schema = AgentOutputSchema(AgentDecisionOutput)

    assert schema.is_plain_text() is False
    assert schema.json_schema()["additionalProperties"] is False
