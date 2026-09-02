"""Reusable contract checks for provider-neutral mutating action adapters."""

from dataclasses import dataclass

from tiramisu_agents.core.contracts.actions import ActionConflict
from tiramisu_agents.core.ports.actions import (
    ActionAdapter,
    DefinitiveActionConflict,
    ProviderActionRequest,
)


@dataclass(frozen=True, slots=True)
class MutatingActionAdapterContract:
    """Adapter-specific fixtures for the common idempotency/conflict contract.

    Client packs choose the resource, allocation rule, and optional expiry
    fixture. The common contract only asserts that a definitive conflict has
    no successful side effect to reconcile or retry.
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
    try:
        await adapter.execute(request)
    except DefinitiveActionConflict as error:
        if error.conflict != expected:
            raise AssertionError(
                "adapter returned a different conflict than its contract fixture"
            ) from error
    else:
        raise AssertionError("adapter did not return a definitive conflict")
    if await adapter.lookup(request.idempotency_key) is not None:
        raise AssertionError("a definitive conflict must not create a successful lookup result")
