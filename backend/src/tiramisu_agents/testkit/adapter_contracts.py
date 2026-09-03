"""Reusable contract checks for provider-neutral mutating action adapters."""

from dataclasses import dataclass

from tiramisu_agents.core.contracts.actions import ActionConflict
from tiramisu_agents.core.ports.actions import (
    ActionAdapter,
    AmbiguousActionOutcome,
    DefinitiveActionConflict,
    DefinitiveActionFailure,
    ProviderActionRequest,
    ProviderActionResult,
)


@dataclass(frozen=True, slots=True)
class MutatingActionAdapterContract:
    """Adapter-specific fixtures for the common idempotency/conflict contract.

    Client packs choose the resource, allocation rule, and optional expiry
    fixture. A conflict is itself a terminal lookup outcome, so recovery never
    needs to repeat the provider operation.
    """

    successful_request: ProviderActionRequest
    conflict_request: ProviderActionRequest
    expected_conflict: ActionConflict
    expired_hold_request: ProviderActionRequest | None = None
    expected_expired_hold_conflict: ActionConflict | None = None


async def assert_mutating_action_adapter_contract(
    adapter: ActionAdapter,
    fixture: MutatingActionAdapterContract,
) -> None:
    """Assert idempotency and definitive conflict behavior for one adapter."""

    initial = await adapter.execute(fixture.successful_request)
    repeated = await adapter.execute(fixture.successful_request)
    if repeated != initial:
        raise AssertionError("adapter did not return the same result for an idempotency retry")
    looked_up = await adapter.lookup(fixture.successful_request.idempotency_key)
    if looked_up != initial:
        raise AssertionError("adapter lookup did not recover the successful idempotent result")

    await _assert_conflict(
        adapter,
        fixture.conflict_request,
        fixture.expected_conflict,
    )
    if fixture.expired_hold_request is not None:
        if fixture.expected_expired_hold_conflict is None:
            raise AssertionError("an expired hold fixture requires its expected conflict")
        await _assert_conflict(
            adapter,
            fixture.expired_hold_request,
            fixture.expected_expired_hold_conflict,
        )
    elif fixture.expected_expired_hold_conflict is not None:
        raise AssertionError("an expiry conflict requires an expired hold request")


async def _assert_conflict(
    adapter: ActionAdapter,
    request: ProviderActionRequest,
    expected: ActionConflict,
) -> None:
    for _ in range(2):
        try:
            await adapter.execute(request)
        except DefinitiveActionConflict as error:
            if error.conflict != expected:
                raise AssertionError(
                    "adapter returned a different conflict than its contract fixture"
                ) from error
        else:
            raise AssertionError("adapter did not return a definitive conflict")
    if await adapter.lookup(request.idempotency_key) != expected:
        raise AssertionError("adapter lookup did not recover the definitive conflict")


async def assert_timeout_after_success_adapter_contract(
    adapter: ActionAdapter,
    request: ProviderActionRequest,
    expected: ProviderActionResult,
) -> None:
    """Assert an ambiguous response can be recovered without another side effect."""

    try:
        await adapter.execute(request)
    except AmbiguousActionOutcome:
        pass
    else:
        raise AssertionError("adapter did not model an ambiguous timeout after success")
    if await adapter.lookup(request.idempotency_key) != expected:
        raise AssertionError("adapter lookup did not recover the ambiguous successful outcome")
    if await adapter.execute(request) != expected:
        raise AssertionError("adapter retry did not return the prior successful outcome")


async def assert_definitive_failure_adapter_contract(
    adapter: ActionAdapter,
    request: ProviderActionRequest,
    *,
    message: str,
) -> None:
    """Assert a definitive failure has no recoverable provider outcome."""

    try:
        await adapter.execute(request)
    except DefinitiveActionConflict as error:
        raise AssertionError("ordinary definitive failure was reported as a conflict") from error
    except DefinitiveActionFailure as error:
        if str(error) != message:
            raise AssertionError("adapter returned a different definitive failure") from error
    else:
        raise AssertionError("adapter did not return a definitive failure")
    if await adapter.lookup(request.idempotency_key) is not None:
        raise AssertionError("definitive failure unexpectedly created a lookup outcome")
