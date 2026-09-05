"""Pure deterministic model token/cost budget policy."""

from dataclasses import dataclass
from enum import StrEnum

from tiramisu_agents.processes.definitions import ProcessDefinition


class ModelBudgetExceeded(ValueError):
    """Raised when deterministic policy forbids another model call for a process."""

    def __init__(self, block: "ModelBudgetBlock") -> None:
        self.block = block
        super().__init__(block.message)


class ModelBudgetBlockCode(StrEnum):
    INPUT_TOKEN_LIMIT = "input_token_limit"
    OUTPUT_TOKEN_LIMIT = "output_token_limit"
    TOTAL_TOKEN_LIMIT = "total_token_limit"
    COST_LIMIT = "cost_limit"
    TENANT_SPEND_LIMIT = "tenant_spend_limit"
    CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True, slots=True)
class ModelBudgetBlock:
    code: ModelBudgetBlockCode
    message: str


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Authoritative token counts for model calls, from provider usage reports."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("input tokens", self.input_tokens),
            ("output tokens", self.output_tokens),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"model {label} must be a non-negative integer")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        if type(other) is not ModelUsage:
            raise TypeError("model usage can only be added to model usage")
        return ModelUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelBudget:
    max_input_tokens_per_process: int
    max_output_tokens_per_process: int
    max_total_tokens_per_process: int
    max_cost_micros_per_process: int

    @classmethod
    def from_definition(cls, definition: ProcessDefinition) -> "ModelBudget":
        limits = definition.limits
        return cls(
            max_input_tokens_per_process=limits.max_model_input_tokens_per_process,
            max_output_tokens_per_process=limits.max_model_output_tokens_per_process,
            max_total_tokens_per_process=limits.max_model_total_tokens_per_process,
            max_cost_micros_per_process=limits.max_model_cost_micros_per_process,
        )


@dataclass(frozen=True, slots=True)
class ModelBudgetSnapshot:
    evaluated_spent: ModelUsage
    evaluated_cost_micros: int
    model_allowed_now: bool
    blocks: tuple[ModelBudgetBlock, ...]

    def require_allowed(self) -> None:
        if self.blocks:
            raise ModelBudgetExceeded(self.blocks[0])


def evaluate_model_budget(
    *,
    budget: ModelBudget,
    spent: ModelUsage,
    spent_cost_micros: int,
) -> ModelBudgetSnapshot:
    """Decide whether a process may make another model call.

    Prospective usage is unknowable before the call, so the fence blocks
    once recorded spend reaches a cap. A single call may overshoot; the
    overshoot is recorded and the next call is then forbidden.
    """

    if type(spent_cost_micros) is not int or spent_cost_micros < 0:
        raise ValueError("spent model cost must be a non-negative integer of micro-USD")

    blocks: list[ModelBudgetBlock] = []
    if spent.input_tokens >= budget.max_input_tokens_per_process:
        blocks.append(
            ModelBudgetBlock(
                ModelBudgetBlockCode.INPUT_TOKEN_LIMIT,
                "process model input-token budget has been reached",
            )
        )
    if spent.output_tokens >= budget.max_output_tokens_per_process:
        blocks.append(
            ModelBudgetBlock(
                ModelBudgetBlockCode.OUTPUT_TOKEN_LIMIT,
                "process model output-token budget has been reached",
            )
        )
    if spent.total_tokens >= budget.max_total_tokens_per_process:
        blocks.append(
            ModelBudgetBlock(
                ModelBudgetBlockCode.TOTAL_TOKEN_LIMIT,
                "process model total-token budget has been reached",
            )
        )
    if spent_cost_micros >= budget.max_cost_micros_per_process:
        blocks.append(
            ModelBudgetBlock(
                ModelBudgetBlockCode.COST_LIMIT,
                "process model cost budget has been reached",
            )
        )
    return ModelBudgetSnapshot(
        evaluated_spent=spent,
        evaluated_cost_micros=spent_cost_micros,
        model_allowed_now=not blocks,
        blocks=tuple(blocks),
    )
