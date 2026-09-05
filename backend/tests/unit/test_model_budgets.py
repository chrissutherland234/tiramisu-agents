"""Integration-free model token/cost budget boundary tests."""

import pytest
from tiramisu_agents.budgets import (
    BreakerScope,
    ModelBudget,
    ModelBudgetBlockCode,
    ModelBudgetExceeded,
    ModelPriceOverride,
    ModelUsage,
    PriceEntry,
    TenantSpendCaps,
    UnknownModelPrice,
    estimate_cost_micros,
    evaluate_model_budget,
    evaluate_tenant_spend,
    normalize_breaker_target,
    require_priced_model,
    resolve_model_prices,
)
from tiramisu_agents.builtin import load_fictional_deployment


def _budget(**changes: object) -> ModelBudget:
    values: dict[str, object] = {
        "max_input_tokens_per_process": 1_000,
        "max_output_tokens_per_process": 500,
        "max_total_tokens_per_process": 1_200,
        "max_cost_micros_per_process": 1_000_000,
    }
    values.update(changes)
    return ModelBudget(**values)  # type: ignore[arg-type]


def test_empty_spend_is_allowed() -> None:
    snapshot = evaluate_model_budget(budget=_budget(), spent=ModelUsage(), spent_cost_micros=0)

    assert snapshot.model_allowed_now is True
    assert snapshot.blocks == ()


def test_exact_spend_blocks_with_stable_priority_order() -> None:
    snapshot = evaluate_model_budget(
        budget=_budget(),
        spent=ModelUsage(input_tokens=1_000, output_tokens=500),
        spent_cost_micros=1_000_000,
    )

    assert snapshot.model_allowed_now is False
    assert [block.code for block in snapshot.blocks] == [
        ModelBudgetBlockCode.INPUT_TOKEN_LIMIT,
        ModelBudgetBlockCode.OUTPUT_TOKEN_LIMIT,
        ModelBudgetBlockCode.TOTAL_TOKEN_LIMIT,
        ModelBudgetBlockCode.COST_LIMIT,
    ]


def test_one_token_below_every_cap_is_still_allowed() -> None:
    snapshot = evaluate_model_budget(
        budget=_budget(
            max_input_tokens_per_process=1_001,
            max_output_tokens_per_process=501,
            max_total_tokens_per_process=1_501,
            max_cost_micros_per_process=1_000_001,
        ),
        spent=ModelUsage(input_tokens=1_000, output_tokens=500),
        spent_cost_micros=1_000_000,
    )

    assert snapshot.model_allowed_now is True


def test_each_cap_blocks_independently() -> None:
    assert (
        evaluate_model_budget(
            budget=_budget(), spent=ModelUsage(input_tokens=1_000), spent_cost_micros=0
        )
        .blocks[0]
        .code
        is ModelBudgetBlockCode.INPUT_TOKEN_LIMIT
    )
    assert (
        evaluate_model_budget(
            budget=_budget(), spent=ModelUsage(output_tokens=500), spent_cost_micros=0
        )
        .blocks[0]
        .code
        is ModelBudgetBlockCode.OUTPUT_TOKEN_LIMIT
    )
    assert (
        evaluate_model_budget(
            budget=_budget(
                max_input_tokens_per_process=10_000,
                max_output_tokens_per_process=10_000,
            ),
            spent=ModelUsage(input_tokens=600, output_tokens=600),
            spent_cost_micros=0,
        )
        .blocks[0]
        .code
        is ModelBudgetBlockCode.TOTAL_TOKEN_LIMIT
    )
    assert (
        evaluate_model_budget(budget=_budget(), spent=ModelUsage(), spent_cost_micros=1_000_000)
        .blocks[0]
        .code
        is ModelBudgetBlockCode.COST_LIMIT
    )


def test_require_allowed_raises_with_the_first_block() -> None:
    snapshot = evaluate_model_budget(
        budget=_budget(), spent=ModelUsage(input_tokens=1_000), spent_cost_micros=0
    )

    with pytest.raises(ModelBudgetExceeded) as error:
        snapshot.require_allowed()
    assert error.value.block.code is ModelBudgetBlockCode.INPUT_TOKEN_LIMIT


def test_usage_arithmetic_and_validation() -> None:
    combined = ModelUsage(input_tokens=10, output_tokens=5) + ModelUsage(
        input_tokens=3, output_tokens=7
    )

    assert combined.total_tokens == 25
    with pytest.raises(ValueError, match="non-negative integer"):
        ModelUsage(input_tokens=-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        ModelUsage(output_tokens=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative integer"):
        evaluate_model_budget(budget=_budget(), spent=ModelUsage(), spent_cost_micros=-1)


def test_budget_compiles_from_the_definition_limits() -> None:
    definition = load_fictional_deployment().definition

    budget = ModelBudget.from_definition(definition)

    assert budget.max_input_tokens_per_process == (
        definition.limits.max_model_input_tokens_per_process
    )
    assert budget.max_cost_micros_per_process == (
        definition.limits.max_model_cost_micros_per_process
    )
    assert "Model budget" in definition.compile_instructions()


def test_cost_estimate_uses_per_million_rates_rounded_down() -> None:
    usage = ModelUsage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert estimate_cost_micros("gpt-4o-mini", usage) == 750_000
    assert (
        estimate_cost_micros(
            "custom",
            ModelUsage(input_tokens=1_500_000, output_tokens=0),
            prices={"custom": PriceEntry(1_000_000, 0)},
        )
        == 1_500_000
    )
    assert estimate_cost_micros("gpt-4o-mini", ModelUsage(input_tokens=3)) == 0


def test_cost_estimate_rejects_blank_and_unpriced_models() -> None:
    with pytest.raises(ValueError, match="model name is required"):
        estimate_cost_micros("  ", ModelUsage(input_tokens=1))
    with pytest.raises(UnknownModelPrice, match="no price entry"):
        estimate_cost_micros("future-model-9", ModelUsage(input_tokens=1))


def test_tenant_spend_fence_blocks_tokens_and_cost_independently() -> None:
    caps = TenantSpendCaps(max_tokens=1_000, max_cost_micros=500)

    assert (
        evaluate_tenant_spend(
            spent=ModelUsage(input_tokens=600, output_tokens=400), spent_cost_micros=0, caps=caps
        )
        is not None
    )
    cost_block = evaluate_tenant_spend(spent=ModelUsage(), spent_cost_micros=500, caps=caps)
    assert cost_block is not None
    assert cost_block.code is ModelBudgetBlockCode.TENANT_SPEND_LIMIT
    assert (
        evaluate_tenant_spend(spent=ModelUsage(input_tokens=999), spent_cost_micros=499, caps=caps)
        is None
    )


def test_breaker_targets_are_canonical_per_scope() -> None:
    assert normalize_breaker_target(BreakerScope.CAPABILITY, "  send_message ") == "send_message"
    assert normalize_breaker_target(BreakerScope.ALL, "") == ""
    with pytest.raises(ValueError, match="non-blank action type"):
        normalize_breaker_target(BreakerScope.CAPABILITY, "   ")
    with pytest.raises(ValueError, match="only capability breakers"):
        normalize_breaker_target(BreakerScope.MODEL_CALLS, "send_message")


def test_price_overrides_win_and_blank_names_are_rejected() -> None:
    resolved = resolve_model_prices(
        {
            "gpt-4o-mini": ModelPriceOverride(
                input_micros_per_million_tokens=1,
                output_micros_per_million_tokens=2,
            )
        }
    )

    assert (
        estimate_cost_micros(
            "gpt-4o-mini",
            ModelUsage(input_tokens=1_000_000, output_tokens=1_000_000),
            prices=resolved,
        )
        == 3
    )
    require_priced_model("gpt-4o-mini", resolved)
    with pytest.raises(UnknownModelPrice, match="no price entry"):
        require_priced_model("future-model-9", resolved)
    with pytest.raises(ValueError, match="non-blank model name"):
        resolve_model_prices(
            {
                "  ": ModelPriceOverride(
                    input_micros_per_million_tokens=1, output_micros_per_million_tokens=1
                )
            }
        )
