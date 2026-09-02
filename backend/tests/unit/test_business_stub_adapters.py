"""Contract and business-invariant tests for the stateful fictional providers."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tiramisu_agents.adapters.stubs import (
    StubBookingAdapter,
    StubBusinessState,
    StubCalendarAdapter,
    StubMessagingAdapter,
    StubOutboundMessage,
    StubPaymentAdapter,
)
from tiramisu_agents.core.contracts.actions import ActionConflict
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.ports.actions import DefinitiveActionFailure, ProviderActionRequest
from tiramisu_agents.testkit import (
    MutatingActionAdapterContract,
    assert_mutating_action_adapter_contract,
)


@pytest.mark.asyncio
async def test_booking_rejects_a_slot_the_provider_did_not_offer() -> None:
    state = StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC))
    adapter = StubBookingAdapter(state)

    with pytest.raises(DefinitiveActionFailure, match="not available"):
        await adapter.execute(
            ProviderActionRequest(
                action_type="propose_booking",
                parameters={
                    "customer_id": "customer-1",
                    "slot": "2026-10-01T10:00:00+00:00",
                },
                idempotency_key="a" * 64,
            )
        )

    assert state.bookings == {}


@pytest.mark.asyncio
async def test_booking_reports_a_typed_conflict_for_a_slot_taken_after_availability() -> None:
    state = StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC))
    adapter = StubBookingAdapter(state)
    selected_slot, *other_slots = state.available_slots
    await assert_mutating_action_adapter_contract(
        adapter,
        MutatingActionAdapterContract(
            successful_request=ProviderActionRequest(
                action_type="propose_booking",
                parameters={"customer_id": "first-customer", "slot": selected_slot},
                idempotency_key="b" * 64,
            ),
            conflict_request=ProviderActionRequest(
                action_type="propose_booking",
                parameters={"customer_id": "second-customer", "slot": selected_slot},
                idempotency_key="c" * 64,
                authoritative_facts={"booking.available_slots": list(state.available_slots)},
            ),
            expected_conflict=ActionConflict(
                code="resource_unavailable",
                message="the requested booking slot is no longer available",
                details={"resource_type": "appointment_slot", "selected_slot": selected_slot},
                facts=(
                    FactObservation(
                        key="booking.available_slots",
                        kind=FactKind.AUTHORITATIVE,
                        value=other_slots,
                    ),
                ),
            ),
        ),
    )
    available = await adapter.execute(
        ProviderActionRequest(
            action_type="find_available_slots",
            parameters={"days": 7},
            idempotency_key="d" * 64,
        )
    )
    assert available.result["slots"] == list(other_slots)


@pytest.mark.asyncio
async def test_calendar_requires_completed_payment() -> None:
    state = StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC))
    booking_adapter = StubBookingAdapter(state)
    payment_adapter = StubPaymentAdapter(state)
    calendar_adapter = StubCalendarAdapter(state)
    slot = state.available_slots[0]
    booking = await booking_adapter.execute(
        ProviderActionRequest(
            action_type="propose_booking",
            parameters={"customer_id": "customer-1", "slot": slot},
            idempotency_key="b" * 64,
        )
    )
    assert booking.result["status"] == "confirmed"
    assert state.bookings[booking.provider_reference].status == "confirmed"
    calendar_request = ProviderActionRequest(
        action_type="create_calendar_event",
        parameters={
            "booking_reference": booking.provider_reference,
            "starts_at": slot,
            "title": "Customer booking",
        },
        idempotency_key="c" * 64,
    )

    assert state.bookings[booking.provider_reference].status == "confirmed"
    payment = await payment_adapter.execute(
        ProviderActionRequest(
            action_type="request_payment",
            parameters={
                "booking_reference": booking.provider_reference,
                "amount_minor": 10_000,
                "currency": "NZD",
            },
            idempotency_key="d" * 64,
        )
    )
    with pytest.raises(DefinitiveActionFailure, match="completed payment"):
        await calendar_adapter.execute(calendar_request)

    payment_event = state.complete_payment(
        tenant_id=uuid4(),
        process_instance_id=uuid4(),
        payment_reference=payment.provider_reference,
    )
    state.payment_requests[payment.provider_reference].status = "pending"
    state.apply_event(payment_event)
    created = await calendar_adapter.execute(calendar_request)
    retried = await calendar_adapter.execute(calendar_request)

    assert retried == created
    assert len(state.calendar_events) == 1

    reloaded_state = StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC))
    reloaded_state.apply_event(
        payment_event,
        authoritative_facts={"booking.slot": slot, "customer.email": "customer-1"},
    )
    reloaded = await StubCalendarAdapter(reloaded_state).execute(calendar_request)
    assert reloaded.result["created"] is True


@pytest.mark.asyncio
async def test_messaging_requires_the_canonical_recipient_parameter() -> None:
    state = StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC))
    adapter = StubMessagingAdapter(state)

    with pytest.raises(DefinitiveActionFailure, match="recipient must be a nonblank string"):
        await adapter.execute(
            ProviderActionRequest(
                action_type="send_message",
                parameters={"recipient_email": "customer@example.test", "body": "Hello"},
                idempotency_key="e" * 64,
            )
        )


def test_stub_provider_events_preserve_process_and_business_references() -> None:
    state = StubBusinessState(now=datetime(2026, 9, 1, 9, tzinfo=UTC))
    tenant_id = uuid4()
    process_id = uuid4()
    state.outbound_messages["msg-1"] = StubOutboundMessage(
        reference="msg-1",
        recipient="customer@example.test",
        body="Hello",
        sent_at=state.now,
    )

    reply = state.customer_reply(
        tenant_id=tenant_id,
        process_instance_id=process_id,
        message_reference="msg-1",
        content="Tuesday please.",
    )

    assert reply.tenant_id == tenant_id
    assert reply.process_instance_id == process_id
    assert reply.external_references[0].external_id == "msg-1"
