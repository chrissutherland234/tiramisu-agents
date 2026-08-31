"""Stateful deterministic business-provider adapters for reference journeys."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.ports.actions import (
    ActionAdapter,
    DefinitiveActionFailure,
    ProviderActionRequest,
    ProviderActionResult,
)


def _require_string(parameters: dict[str, Any], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DefinitiveActionFailure(f"{name} must be a nonblank string")
    return value


def _require_positive_int(parameters: dict[str, Any], name: str) -> int:
    value = parameters.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DefinitiveActionFailure(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class StubOutboundMessage:
    reference: str
    recipient: str
    body: str
    sent_at: datetime


@dataclass(slots=True)
class StubBooking:
    reference: str
    customer_id: str
    slot: str
    status: str = "proposed"


@dataclass(slots=True)
class StubPaymentRequest:
    reference: str
    booking_reference: str
    amount_minor: int
    currency: str
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class StubCalendarEvent:
    reference: str
    booking_reference: str
    starts_at: str
    title: str


def _new_outbound_messages() -> dict[str, StubOutboundMessage]:
    return {}


def _new_bookings() -> dict[str, StubBooking]:
    return {}


def _new_payment_requests() -> dict[str, StubPaymentRequest]:
    return {}


def _new_calendar_events() -> dict[str, StubCalendarEvent]:
    return {}


@dataclass(slots=True)
class StubBusinessState:
    """Shared fictional provider state with deterministic inbound event helpers."""

    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    available_slots: tuple[str, ...] = ()
    outbound_messages: dict[str, StubOutboundMessage] = field(
        default_factory=_new_outbound_messages
    )
    bookings: dict[str, StubBooking] = field(default_factory=_new_bookings)
    payment_requests: dict[str, StubPaymentRequest] = field(default_factory=_new_payment_requests)
    calendar_events: dict[str, StubCalendarEvent] = field(default_factory=_new_calendar_events)

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("stub business clock must be timezone-aware")
        if not self.available_slots:
            self.available_slots = tuple(
                (self.now + timedelta(days=offset)).replace(hour=10, minute=0, second=0).isoformat()
                for offset in (2, 3, 4)
            )

    def advance(self, duration: timedelta) -> None:
        if duration < timedelta(0):
            raise ValueError("stub business clock cannot move backwards")
        self.now += duration

    def apply_event(
        self,
        event: CanonicalEvent,
        *,
        authoritative_facts: Mapping[str, Any] | None = None,
    ) -> None:
        """Reconcile provider-local state from an inbound fictional event.

        Local API event injection updates the durable process projection, while
        the worker's fictional providers keep a small in-memory state of their
        own. Applying provider completion events here keeps those two views in
        sync before the next action is executed.
        """
        if event.event_type != "payment.completed":
            return
        facts = {observation.key: observation.value for observation in event.facts}
        payment_reference = facts.get("payment.reference") or event.payload.get("payment_reference")
        if not isinstance(payment_reference, str):
            return
        payment = self.payment_requests.get(payment_reference)
        if payment is not None:
            payment.status = "completed"
            return

        payload = event.payload
        booking_reference = payload.get("booking_reference")
        amount_minor = facts.get("payment.amount_minor")
        currency = facts.get("payment.currency")
        if not (
            isinstance(booking_reference, str)
            and isinstance(amount_minor, int)
            and not isinstance(amount_minor, bool)
            and isinstance(currency, str)
        ):
            return

        known_facts = authoritative_facts or {}
        booking_slot = known_facts.get("booking.slot")
        customer_id = known_facts.get("customer.email", booking_reference)
        if booking_reference not in self.bookings and isinstance(booking_slot, str):
            self.bookings[booking_reference] = StubBooking(
                reference=booking_reference,
                customer_id=(customer_id if isinstance(customer_id, str) else booking_reference),
                slot=booking_slot,
                status="confirmed",
            )
        self.payment_requests[payment_reference] = StubPaymentRequest(
            reference=payment_reference,
            booking_reference=booking_reference,
            amount_minor=amount_minor,
            currency=currency.upper(),
            status="completed",
        )

    def customer_reply(
        self,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        message_reference: str,
        content: str,
    ) -> CanonicalEvent:
        if message_reference not in self.outbound_messages:
            raise LookupError("outbound message not found")
        if not content.strip():
            raise ValueError("customer reply cannot be blank")
        return CanonicalEvent(
            tenant_id=tenant_id,
            process_instance_id=process_instance_id,
            event_type="customer.email_received",
            source="stub.messaging.v1",
            source_event_id=f"reply:{message_reference}",
            occurred_at=self.now,
            external_references=(
                ExternalReference(
                    provider="stub.messaging.v1",
                    resource_type="message",
                    external_id=message_reference,
                ),
            ),
            facts=(
                FactObservation(
                    key="customer.last_message",
                    kind=FactKind.CUSTOMER_CLAIM,
                    value=content,
                ),
            ),
            payload={"content": content, "in_reply_to": message_reference},
        )

    def confirm_booking(
        self,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        booking_reference: str,
    ) -> CanonicalEvent:
        booking = self.bookings.get(booking_reference)
        if booking is None:
            raise LookupError("booking not found")
        booking.status = "confirmed"
        return CanonicalEvent(
            tenant_id=tenant_id,
            process_instance_id=process_instance_id,
            event_type="booking.confirmed",
            source="stub.booking.v1",
            source_event_id=f"confirmed:{booking_reference}",
            occurred_at=self.now,
            external_references=(
                ExternalReference(
                    provider="stub.booking.v1",
                    resource_type="booking",
                    external_id=booking_reference,
                ),
            ),
            facts=(
                FactObservation(
                    key="booking.reference",
                    kind=FactKind.AUTHORITATIVE,
                    value=booking_reference,
                ),
                FactObservation(
                    key="booking.status",
                    kind=FactKind.AUTHORITATIVE,
                    value="confirmed",
                ),
                FactObservation(
                    key="booking.slot",
                    kind=FactKind.AUTHORITATIVE,
                    value=booking.slot,
                ),
            ),
            payload={"booking_reference": booking_reference, "slot": booking.slot},
        )

    def complete_payment(
        self,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        payment_reference: str,
    ) -> CanonicalEvent:
        payment = self.payment_requests.get(payment_reference)
        if payment is None:
            raise LookupError("payment request not found")
        payment.status = "completed"
        return CanonicalEvent(
            tenant_id=tenant_id,
            process_instance_id=process_instance_id,
            event_type="payment.completed",
            source="stub.payment.v1",
            source_event_id=f"completed:{payment_reference}",
            occurred_at=self.now,
            external_references=(
                ExternalReference(
                    provider="stub.payment.v1",
                    resource_type="payment",
                    external_id=payment_reference,
                ),
                ExternalReference(
                    provider="stub.booking.v1",
                    resource_type="booking",
                    external_id=payment.booking_reference,
                ),
            ),
            facts=(
                FactObservation(
                    key="payment.reference",
                    kind=FactKind.AUTHORITATIVE,
                    value=payment_reference,
                ),
                FactObservation(
                    key="payment.status",
                    kind=FactKind.AUTHORITATIVE,
                    value="completed",
                ),
                FactObservation(
                    key="payment.amount_minor",
                    kind=FactKind.AUTHORITATIVE,
                    value=payment.amount_minor,
                ),
                FactObservation(
                    key="payment.currency",
                    kind=FactKind.AUTHORITATIVE,
                    value=payment.currency,
                ),
            ),
            payload={
                "payment_reference": payment_reference,
                "booking_reference": payment.booking_reference,
                "amount_minor": payment.amount_minor,
                "currency": payment.currency,
            },
        )


class _StatefulActionAdapter:
    guarantees_idempotency = True

    def __init__(self, state: StubBusinessState) -> None:
        self.state = state
        self.requests: list[ProviderActionRequest] = []
        self._results: dict[str, ProviderActionResult] = {}

    async def execute(self, request: ProviderActionRequest) -> ProviderActionResult:
        self.requests.append(request)
        existing = self._results.get(request.idempotency_key)
        if existing is not None:
            return existing
        result = self._perform(request)
        self._results[request.idempotency_key] = result
        return result

    async def lookup(self, idempotency_key: str) -> ProviderActionResult | None:
        return self._results.get(idempotency_key)

    def _perform(self, request: ProviderActionRequest) -> ProviderActionResult:
        raise NotImplementedError


class StubMessagingAdapter(_StatefulActionAdapter):
    id = "stub.messaging.v1"

    def _perform(self, request: ProviderActionRequest) -> ProviderActionResult:
        if request.action_type != "send_message":
            raise DefinitiveActionFailure("messaging adapter only supports send_message")
        recipient = _require_string(request.parameters, "recipient")
        body = _require_string(request.parameters, "body")
        reference = f"msg_{request.idempotency_key[:16]}"
        self.state.outbound_messages[reference] = StubOutboundMessage(
            reference=reference,
            recipient=recipient,
            body=body,
            sent_at=self.state.now,
        )
        return ProviderActionResult(
            provider_reference=reference,
            result={"message_reference": reference, "sent": True},
            facts=(
                FactObservation(
                    key="messaging.last_outbound_reference",
                    kind=FactKind.AUTHORITATIVE,
                    value=reference,
                ),
            ),
        )


class StubBookingAdapter(_StatefulActionAdapter):
    id = "stub.booking.v1"

    def _perform(self, request: ProviderActionRequest) -> ProviderActionResult:
        if request.action_type == "find_available_slots":
            days = _require_positive_int(request.parameters, "days")
            horizon = self.state.now + timedelta(days=days)
            slots = tuple(
                slot
                for slot in self.state.available_slots
                if datetime.fromisoformat(slot) <= horizon
            )
            return ProviderActionResult(
                provider_reference=f"availability_{request.idempotency_key[:16]}",
                result={"slots": list(slots)},
                facts=(
                    FactObservation(
                        key="booking.available_slots",
                        kind=FactKind.AUTHORITATIVE,
                        value=list(slots),
                    ),
                ),
            )
        if request.action_type != "propose_booking":
            raise DefinitiveActionFailure(
                "booking adapter supports find_available_slots and propose_booking"
            )
        customer_id = _require_string(request.parameters, "customer_id")
        slot = _require_string(request.parameters, "slot")
        if slot not in self.state.available_slots:
            raise DefinitiveActionFailure("booking slot is not available")
        reference = f"booking_{request.idempotency_key[:16]}"
        self.state.bookings[reference] = StubBooking(
            reference=reference,
            customer_id=customer_id,
            slot=slot,
            status="confirmed",
        )
        return ProviderActionResult(
            provider_reference=reference,
            result={"booking_reference": reference, "status": "confirmed", "slot": slot},
            facts=(
                FactObservation(
                    key="booking.reference",
                    kind=FactKind.AUTHORITATIVE,
                    value=reference,
                ),
                FactObservation(
                    key="booking.status",
                    kind=FactKind.AUTHORITATIVE,
                    value="confirmed",
                ),
                FactObservation(
                    key="booking.slot",
                    kind=FactKind.AUTHORITATIVE,
                    value=slot,
                ),
            ),
        )


class StubPaymentAdapter(_StatefulActionAdapter):
    id = "stub.payment.v1"

    def _perform(self, request: ProviderActionRequest) -> ProviderActionResult:
        if request.action_type != "request_payment":
            raise DefinitiveActionFailure("payment adapter only supports request_payment")
        booking_reference = _require_string(request.parameters, "booking_reference")
        booking = self.state.bookings.get(booking_reference)
        if booking is None or booking.status != "confirmed":
            raise DefinitiveActionFailure("payment requires a confirmed booking")
        amount_minor = _require_positive_int(request.parameters, "amount_minor")
        currency = _require_string(request.parameters, "currency").upper()
        reference = f"payment_{request.idempotency_key[:16]}"
        self.state.payment_requests[reference] = StubPaymentRequest(
            reference=reference,
            booking_reference=booking_reference,
            amount_minor=amount_minor,
            currency=currency,
        )
        return ProviderActionResult(
            provider_reference=reference,
            result={
                "payment_reference": reference,
                "status": "pending",
                "amount_minor": amount_minor,
                "currency": currency,
            },
            facts=(
                FactObservation(
                    key="payment.reference",
                    kind=FactKind.AUTHORITATIVE,
                    value=reference,
                ),
                FactObservation(
                    key="payment.status",
                    kind=FactKind.AUTHORITATIVE,
                    value="pending",
                ),
                FactObservation(
                    key="payment.amount_minor",
                    kind=FactKind.AUTHORITATIVE,
                    value=amount_minor,
                ),
                FactObservation(
                    key="payment.currency",
                    kind=FactKind.AUTHORITATIVE,
                    value=currency,
                ),
            ),
        )


class StubCalendarAdapter(_StatefulActionAdapter):
    id = "stub.calendar.v1"

    def _perform(self, request: ProviderActionRequest) -> ProviderActionResult:
        if request.action_type != "create_calendar_event":
            raise DefinitiveActionFailure("calendar adapter only supports create_calendar_event")
        booking_reference = _require_string(request.parameters, "booking_reference")
        booking = self.state.bookings.get(booking_reference)
        if booking is None or booking.status != "confirmed":
            raise DefinitiveActionFailure("calendar event requires a confirmed booking")
        paid = any(
            payment.booking_reference == booking_reference and payment.status == "completed"
            for payment in self.state.payment_requests.values()
        )
        if not paid:
            raise DefinitiveActionFailure("calendar event requires completed payment")
        starts_at = _require_string(request.parameters, "starts_at")
        if starts_at != booking.slot:
            raise DefinitiveActionFailure("calendar event must use the confirmed booking slot")
        title = _require_string(request.parameters, "title")
        reference = f"calendar_{request.idempotency_key[:16]}"
        self.state.calendar_events[reference] = StubCalendarEvent(
            reference=reference,
            booking_reference=booking_reference,
            starts_at=starts_at,
            title=title,
        )
        return ProviderActionResult(
            provider_reference=reference,
            result={"calendar_reference": reference, "created": True},
            facts=(
                FactObservation(
                    key="calendar.reference",
                    kind=FactKind.AUTHORITATIVE,
                    value=reference,
                ),
                FactObservation(
                    key="calendar.status",
                    kind=FactKind.AUTHORITATIVE,
                    value="created",
                ),
            ),
        )


_messaging_check: ActionAdapter = StubMessagingAdapter(StubBusinessState())
_booking_check: ActionAdapter = StubBookingAdapter(StubBusinessState())
_payment_check: ActionAdapter = StubPaymentAdapter(StubBusinessState())
_calendar_check: ActionAdapter = StubCalendarAdapter(StubBusinessState())


def stub_business_bindings(state: StubBusinessState) -> dict[str, ActionAdapter]:
    """Build the explicit action bindings used by fictional workers and scenarios."""

    messaging = StubMessagingAdapter(state)
    booking = StubBookingAdapter(state)
    payment = StubPaymentAdapter(state)
    calendar = StubCalendarAdapter(state)
    return {
        "send_message": messaging,
        "find_available_slots": booking,
        "propose_booking": booking,
        "request_payment": payment,
        "create_calendar_event": calendar,
    }
