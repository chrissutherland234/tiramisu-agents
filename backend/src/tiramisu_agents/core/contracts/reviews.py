"""Human review commands with explicit semantics."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tiramisu_agents.core.limits import DEFAULT_PLATFORM_SAFETY_LIMITS, require_utf8_bytes


class ReviewCommandType(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_REVISION = "request_revision"
    COMMENT = "comment"
    CORRECT_FACT = "correct_fact"
    CANCEL = "cancel"


class ReviewCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    process_instance_id: UUID
    review_thread_id: UUID
    action_request_id: UUID
    proposal_revision: int = Field(ge=1)
    command_type: ReviewCommandType
    actor_id: UUID
    message: str | None = Field(default=None, max_length=10_000)
    expected_payload_hash: str | None = Field(default=None, min_length=32, max_length=128)

    @field_validator("message")
    @classmethod
    def require_bounded_message(cls, value: str | None) -> str | None:
        if value is not None:
            require_utf8_bytes(
                value,
                label="review message",
                max_bytes=DEFAULT_PLATFORM_SAFETY_LIMITS.max_review_message_bytes,
            )
        return value

    @model_validator(mode="after")
    def require_command_specific_fields(self) -> "ReviewCommand":
        if self.command_type is ReviewCommandType.APPROVE and not self.expected_payload_hash:
            raise ValueError("approval requires the exact expected payload hash")
        if (
            self.command_type
            in {
                ReviewCommandType.REJECT,
                ReviewCommandType.REQUEST_REVISION,
                ReviewCommandType.COMMENT,
                ReviewCommandType.CORRECT_FACT,
            }
            and not self.message
        ):
            raise ValueError(f"{self.command_type.value} requires a message")
        return self
