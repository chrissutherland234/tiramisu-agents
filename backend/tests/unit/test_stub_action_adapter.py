"""Provider-neutral contract checks for the deterministic stub adapter."""

import pytest
from tiramisu_agents.adapters.stubs import StubActionAdapter, StubAmbiguousSuccess
from tiramisu_agents.core.ports.actions import (
    DefinitiveActionFailure,
    ProviderActionRequest,
    ProviderActionResult,
)
from tiramisu_agents.testkit import (
    assert_definitive_failure_adapter_contract,
    assert_timeout_after_success_adapter_contract,
)


@pytest.mark.asyncio
async def test_stub_adapter_is_idempotent_for_a_stable_execution_key() -> None:
    adapter = StubActionAdapter()
    request = ProviderActionRequest(
        action_type="send_message",
        parameters={"recipient": "customer@example.test"},
        idempotency_key="a" * 64,
    )

    first = await adapter.execute(request)
    retried = await adapter.execute(request)

    assert retried == first
    assert len(adapter.requests) == 2
    assert await adapter.lookup(request.idempotency_key) == first


@pytest.mark.asyncio
async def test_stub_adapter_can_model_timeout_after_provider_success() -> None:
    provider_result = ProviderActionResult(
        provider_reference="message-123",
        result={"sent": True},
    )
    adapter = StubActionAdapter([StubAmbiguousSuccess(provider_result)])
    request = ProviderActionRequest(
        action_type="send_message",
        parameters={"recipient": "customer@example.test"},
        idempotency_key="b" * 64,
    )

    await assert_timeout_after_success_adapter_contract(adapter, request, provider_result)
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_stub_adapter_models_a_definitive_failure_without_an_outcome() -> None:
    adapter = StubActionAdapter([DefinitiveActionFailure("provider rejected the request")])
    request = ProviderActionRequest(
        action_type="send_message",
        parameters={"recipient": "customer@example.test"},
        idempotency_key="c" * 64,
    )

    await assert_definitive_failure_adapter_contract(
        adapter,
        request,
        message="provider rejected the request",
    )
