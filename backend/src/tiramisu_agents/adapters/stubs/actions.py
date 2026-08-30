"""Deterministic idempotent action adapter for kernel and scenario tests."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from tiramisu_agents.core.ports.actions import (
    ActionAdapter,
    AmbiguousActionOutcome,
    ProviderActionRequest,
    ProviderActionResult,
)


@dataclass(frozen=True, slots=True)
class StubAmbiguousSuccess:
    result: ProviderActionResult


class StubActionAdapter:
    id = "stub.actions.v1"
    guarantees_idempotency = True

    def __init__(
        self,
        outcomes: Iterable[ProviderActionResult | StubAmbiguousSuccess | Exception] = (),
    ) -> None:
        self._outcomes = deque(outcomes)
        self._results: dict[str, ProviderActionResult] = {}
        self.requests: list[ProviderActionRequest] = []

    async def execute(self, request: ProviderActionRequest) -> ProviderActionResult:
        self.requests.append(request)
        existing = self._results.get(request.idempotency_key)
        if existing is not None:
            return existing
        outcome = (
            self._outcomes.popleft()
            if self._outcomes
            else ProviderActionResult(
                provider_reference=f"stub:{request.idempotency_key[:16]}",
                result={"accepted": True, "action_type": request.action_type},
            )
        )
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, StubAmbiguousSuccess):
            self._results[request.idempotency_key] = outcome.result
            raise AmbiguousActionOutcome("stub accepted the action before the connection failed")
        self._results[request.idempotency_key] = outcome
        return outcome

    async def lookup(self, idempotency_key: str) -> ProviderActionResult | None:
        return self._results.get(idempotency_key)


_adapter_check: ActionAdapter = StubActionAdapter()
