"""Event identities owned by the durable agent kernel, not business adapters."""

OPERATOR_MANUAL_WAKE_EVENT_TYPE = "operator.manual_wake"

RESERVED_KERNEL_EVENT_TYPES = frozenset({OPERATOR_MANUAL_WAKE_EVENT_TYPE})
