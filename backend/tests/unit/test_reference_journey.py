"""Complete integration-free enquiry-to-completion reference scenario."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.adapters.stubs import (
    StubBookingAdapter,
    StubBusinessState,
    StubCalendarAdapter,
    StubMessagingAdapter,
    StubPaymentAdapter,
    stub_business_bindings,
)
from tiramisu_agents.core.contracts.decisions import ActionProposal
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.testkit import (
    FictionalJourneyDriver,
    ScenarioActionStatus,
    new_scenario_identity,
    run_enquiry_to_completion,
)


@pytest.mark.asyncio
async def test_reference_journey_runs_every_primitive_to_completion() -> None:
    definition = ProcessDefinitionRegistry.from_yaml_files(
        [Path("process_definitions/examples/enquiry_to_booking.v1.yaml")]
    ).get("enquiry_to_booking", "1")
    state = StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC))
    messaging = StubMessagingAdapter(state)
    booking = StubBookingAdapter(state)
    payment = StubPaymentAdapter(state)
    calendar = StubCalendarAdapter(state)
    tenant_id, process_id = new_scenario_identity()
    driver = FictionalJourneyDriver(
        tenant_id=tenant_id,
        process_instance_id=process_id,
        definition=definition,
        adapters=ActionAdapterRegistry(
            {
                "send_message": messaging,
                "find_available_slots": booking,
                "propose_booking": booking,
                "request_payment": payment,
                "create_calendar_event": calendar,
            }
        ),
        state=state,
    )

    result = await run_enquiry_to_completion(driver)

    assert result.completed is True
    assert result.event_types == (
        "enquiry.created",
        "customer.email_received",
        "payment.completed",
    )
    assert result.action_types == (
        "send_message",
        "find_available_slots",
        "propose_booking",
        "request_payment",
        "create_calendar_event",
    )
    assert result.approval_count == 3
    assert len(state.outbound_messages) == 1
    assert state.bookings[result.booking_reference].status == "confirmed"
    assert state.payment_requests[result.payment_reference].status == "completed"
    assert state.calendar_events[result.calendar_reference].booking_reference == (
        result.booking_reference
    )
    assert all(action.status is ScenarioActionStatus.SUCCEEDED for action in driver.actions)
    assert len(messaging.requests) == 1
    assert len(booking.requests) == 2
    assert len(payment.requests) == 1
    assert len(calendar.requests) == 1


def test_fictional_worker_bindings_cover_every_configured_action() -> None:
    definition = ProcessDefinitionRegistry.from_yaml_files(
        [Path("process_definitions/examples/enquiry_to_booking.v1.yaml")]
    ).get("enquiry_to_booking", "1")

    bindings = stub_business_bindings(StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC)))

    assert set(bindings) == set(definition.allowed_actions)


@pytest.mark.asyncio
async def test_scenario_driver_does_not_execute_an_action_before_approval() -> None:
    definition = ProcessDefinitionRegistry.from_yaml_files(
        [Path("process_definitions/examples/enquiry_to_booking.v1.yaml")]
    ).get("enquiry_to_booking", "1")
    state = StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC))
    bindings = stub_business_bindings(state)
    tenant_id, process_id = new_scenario_identity()
    driver = FictionalJourneyDriver(
        tenant_id=tenant_id,
        process_instance_id=process_id,
        definition=definition,
        adapters=ActionAdapterRegistry(bindings),
        state=state,
    )
    action = await driver.submit_action(
        ActionProposal(
            logical_action_key="reply",
            action_type="send_message",
            parameters={"recipient": "customer@example.test", "body": "Hello"},
            rationale="Reply to the customer.",
        )
    )

    assert action.status is ScenarioActionStatus.PENDING_APPROVAL
    assert state.outbound_messages == {}

    approved = await driver.approve_action("reply")
    retried = await driver.approve_action("reply")

    assert approved.status is ScenarioActionStatus.SUCCEEDED
    assert retried is approved
    assert len(state.outbound_messages) == 1
