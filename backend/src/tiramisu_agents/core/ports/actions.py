"""Provider-neutral action execution contracts."""

from dataclasses import dataclass
from typing import Any, Protocol

from tiramisu_agents.core.contracts.knowledge import FactObservation


@dataclass(frozen=True, slots=True)
class ProviderActionRequest:
    action_type: str
    parameters: dict[str, Any]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ProviderActionResult:
    provider_reference: str
    result: dict[str, Any]
    facts: tuple[FactObservation, ...] = ()


class ActionAdapter(Protocol):
    id: str
    guarantees_idempotency: bool

    async def execute(self, request: ProviderActionRequest) -> ProviderActionResult: ...

    async def lookup(self, idempotency_key: str) -> ProviderActionResult | None: ...


class DefinitiveActionFailure(RuntimeError):
    """The provider definitively rejected or did not perform an action."""


class AmbiguousActionOutcome(RuntimeError):
    """The provider may have performed the action despite the raised error."""
