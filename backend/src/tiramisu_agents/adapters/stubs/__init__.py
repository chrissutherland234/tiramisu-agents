"""Deterministic provider stubs for scenario tests."""

from tiramisu_agents.adapters.stubs.actions import StubActionAdapter, StubAmbiguousSuccess
from tiramisu_agents.adapters.stubs.business import (
    StubBookingAdapter,
    StubBusinessState,
    StubCalendarAdapter,
    StubCalendarEvent,
    StubMessagingAdapter,
    StubOutboundMessage,
    StubPaymentAdapter,
    StubPaymentRequest,
    stub_business_bindings,
)

__all__ = [
    "StubActionAdapter",
    "StubAmbiguousSuccess",
    "StubBookingAdapter",
    "StubBusinessState",
    "StubCalendarAdapter",
    "StubCalendarEvent",
    "StubMessagingAdapter",
    "StubOutboundMessage",
    "StubPaymentAdapter",
    "StubPaymentRequest",
    "stub_business_bindings",
]
