from uuid import uuid4

import pytest
from tiramisu_agents.budgets.policy import ModelUsage
from tiramisu_agents.core.contracts.decisions import AgentDecision, DecisionStatus
from tiramisu_agents.core.contracts.processes import AgentTurnInput, ProcessSnapshot, ProcessStatus
from tiramisu_agents.testkit import ScriptedAgent


@pytest.mark.asyncio
async def test_scripted_agent_returns_decisions_in_order() -> None:
    expected = AgentDecision(based_on_event_ids=(), status=DecisionStatus.ACTIVE)
    runner = ScriptedAgent(
        [expected], model="gpt-4o-mini", usages=[ModelUsage(input_tokens=7, output_tokens=3)]
    )
    turn_input = AgentTurnInput(
        turn_id=uuid4(),
        process=ProcessSnapshot(
            tenant_id=uuid4(),
            process_instance_id=uuid4(),
            process_type="enquiry_to_booking",
            process_definition_version="1",
            status=ProcessStatus.ACTIVE,
        ),
        events=(),
        instructions="Advance the journey within policy.",
    )

    outcome = await runner.run_turn(turn_input)

    assert outcome.decision == expected
    assert outcome.usage == ModelUsage(input_tokens=7, output_tokens=3)
    assert outcome.model == "gpt-4o-mini"
    assert runner.turn_inputs == [turn_input]
    assert runner.corrections == [None]
