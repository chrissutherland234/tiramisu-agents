"""Complete integration-free enquiry-to-completion executable scenario."""

from datetime import UTC, datetime

import pytest
from tiramisu_agents.adapters.stubs import (
    StubBusinessState,
    stub_business_bindings,
)
from tiramisu_agents.builtin.fictional import (
    FICTIONAL_SCENARIO_STARTED_AT,
    load_fictional_deployment,
)
from tiramisu_agents.testkit import (
    ScenarioTraceKind,
    run_scenario,
)


@pytest.mark.asyncio
async def test_reference_journey_runs_every_primitive_to_completion() -> None:
    state = StubBusinessState(now=FICTIONAL_SCENARIO_STARTED_AT)
    deployment = load_fictional_deployment(state=state)

    result = await run_scenario(deployment, "happy_path")

    assert result.passed is True
    assert result.action_types == (
        "find_available_slots",
        "send_message",
        "propose_booking",
        "request_payment",
        "create_calendar_event",
    )
    assert result.approval_count == 3
    assert len(state.outbound_messages) == 1
    booking_reference = result.authoritative_facts["booking.reference"]
    payment_reference = result.authoritative_facts["payment.reference"]
    calendar_reference = result.authoritative_facts["calendar.reference"]
    assert state.bookings[booking_reference].status == "confirmed"
    assert state.payment_requests[payment_reference].status == "pending"
    assert state.calendar_events[calendar_reference].booking_reference == booking_reference
    approval_positions = [
        index
        for index, entry in enumerate(result.trace)
        if entry.kind is ScenarioTraceKind.APPROVAL
    ]
    result_positions = [
        index for index, entry in enumerate(result.trace) if entry.kind is ScenarioTraceKind.RESULT
    ]
    assert approval_positions[0] < result_positions[1]
    assert approval_positions[1] < result_positions[2]
    assert approval_positions[2] < result_positions[3]


def test_fictional_worker_bindings_cover_every_configured_action() -> None:
    definition = load_fictional_deployment().definition

    bindings = stub_business_bindings(StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC)))

    assert set(bindings) == set(definition.allowed_actions)
