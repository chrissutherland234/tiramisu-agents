from uuid import uuid4

import pytest
from tiramisu_agents.core.contracts.decisions import AgentDecision, DecisionStatus
from tiramisu_agents.core.contracts.processes import AgentTurnInput, ProcessSnapshot, ProcessStatus
from tiramisu_agents.testkit import ScriptedAgent


@pytest.mark.asyncio
async def test_scripted_agent_returns_decisions_in_order() -> None:
    expected = AgentDecision(based_on_event_ids=(), status=DecisionStatus.ACTIVE)
    runner = ScriptedAgent([expected])
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

    assert await runner.run_turn(turn_input) == expected
    assert runner.turn_inputs == [turn_input]
