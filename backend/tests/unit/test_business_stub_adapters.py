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
from tiramisu_agents.core.ports.actions import (
    DefinitiveActionFailure,
    ProviderActionRequest,
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
async def test_calendar_requires_confirmation_and_completed_payment() -> None:
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
    calendar_request = ProviderActionRequest(
        action_type="create_calendar_event",
        parameters={
            "booking_reference": booking.provider_reference,
            "starts_at": slot,
            "title": "Customer booking",
        },
        idempotency_key="c" * 64,
    )

    with pytest.raises(DefinitiveActionFailure, match="confirmed booking"):
        await calendar_adapter.execute(calendar_request)

    state.bookings[booking.provider_reference].status = "confirmed"
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

    state.payment_requests[payment.provider_reference].status = "completed"
    created = await calendar_adapter.execute(calendar_request)
    retried = await calendar_adapter.execute(calendar_request)

    assert retried == created
    assert len(state.calendar_events) == 1


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
