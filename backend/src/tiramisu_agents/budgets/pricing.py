"""Versioned per-model price table for deterministic cost estimates."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from tiramisu_agents.budgets.policy import ModelUsage

PRICE_TABLE_VERSION = 1


class UnknownModelPrice(ValueError):
    """Raised when no price entry exists for the configured model."""


@dataclass(frozen=True, slots=True)
class PriceEntry:
    input_micros_per_million_tokens: int
    output_micros_per_million_tokens: int

    def __post_init__(self) -> None:
        for label, value in (
            ("input price", self.input_micros_per_million_tokens),
            ("output price", self.output_micros_per_million_tokens),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"model {label} must be a non-negative integer")


# Illustrative defaults in micro-USD per million tokens. Refresh against
# provider pricing when models change and bump PRICE_TABLE_VERSION; recorded
# ledger rows keep the version used so history never shifts under review.
DEFAULT_MODEL_PRICES: dict[str, PriceEntry] = {
    "gpt-4o": PriceEntry(2_500_000, 10_000_000),
    "gpt-4o-mini": PriceEntry(150_000, 600_000),
    "gpt-4.1": PriceEntry(2_000_000, 8_000_000),
    "gpt-4.1-mini": PriceEntry(400_000, 1_600_000),
    "o4-mini": PriceEntry(1_100_000, 4_400_000),
}


class ModelPriceOverride(BaseModel):
    """Deployment-provided price entry that wins over the built-in table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_micros_per_million_tokens: int = Field(ge=0, le=10**15)
    output_micros_per_million_tokens: int = Field(ge=0, le=10**15)

    def to_price_entry(self) -> PriceEntry:
        return PriceEntry(
            self.input_micros_per_million_tokens,
            self.output_micros_per_million_tokens,
        )


def resolve_model_prices(
    overrides: Mapping[str, ModelPriceOverride] | None = None,
) -> dict[str, PriceEntry]:
    """Merge deployment overrides over the built-in versioned price table."""

    resolved = dict(DEFAULT_MODEL_PRICES)
    for model, override in (overrides or {}).items():
        if not model.strip():
            raise ValueError("model price overrides require a non-blank model name")
        resolved[model] = override.to_price_entry()
    return resolved


def require_priced_model(model: str, prices: Mapping[str, PriceEntry]) -> None:
    """Fail fast when the configured model has no price entry."""

    if not model.strip():
        raise ValueError("a model name is required to check price coverage")
    if model not in prices:
        raise UnknownModelPrice(f"no price entry for model {model!r}")


def estimate_cost_micros(
    model: str,
    usage: ModelUsage,
    *,
    prices: Mapping[str, PriceEntry] = DEFAULT_MODEL_PRICES,
) -> int:
    """Estimate one call's cost in integer micro-USD, rounding down."""

    if not model.strip():
        raise ValueError("a model name is required to estimate cost")
    try:
        entry = prices[model]
    except KeyError as error:
        raise UnknownModelPrice(f"no price entry for model {model!r}") from error
    return (
        usage.input_tokens * entry.input_micros_per_million_tokens // 1_000_000
        + usage.output_tokens * entry.output_micros_per_million_tokens // 1_000_000
    )
