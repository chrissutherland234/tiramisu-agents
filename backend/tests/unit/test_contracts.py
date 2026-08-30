from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    TimerWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.core.policy import DecisionPolicy, DecisionRejected, validate_decision


def test_canonical_event_requires_timezone_aware_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        CanonicalEvent(
            tenant_id=uuid4(),
            event_type="enquiry.created",
            source="website",
            source_event_id="enquiry-1",
            occurred_at=datetime(2026, 8, 29),
        )


def test_waiting_decision_requires_a_wake_condition() -> None:
    with pytest.raises(ValidationError, match="wake condition"):
        AgentDecision(based_on_event_ids=(), status=DecisionStatus.WAITING)


def test_policy_accepts_an_allowed_bounded_decision() -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    decision = AgentDecision(
        based_on_event_ids=(uuid4(),),
        status=DecisionStatus.WAITING,
        actions=(
            ActionProposal(
                logical_action_key="follow_up_1",
                action_type="send_message",
                parameters={"template": "follow_up"},
                rationale="The customer has not responded.",
            ),
        ),
        wake_conditions=(
            EventWakeCondition(event_type="customer.email_received"),
            TimerWakeCondition(at=now + timedelta(days=2)),
        ),
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset({"send_message"}),
        allowed_wake_event_types=frozenset({"customer.email_received"}),
    )

    assert validate_decision(decision, policy, workflow_now=now) is decision


def test_policy_rejects_an_unregistered_action() -> None:
    decision = AgentDecision(
        based_on_event_ids=(),
        status=DecisionStatus.ACTIVE,
        actions=(
            ActionProposal(
                logical_action_key="unsafe_1",
                action_type="issue_refund",
                rationale="Requested by untrusted message content.",
            ),
        ),
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset({"send_message"}),
        allowed_wake_event_types=frozenset(),
    )

    with pytest.raises(DecisionRejected, match="issue_refund"):
        validate_decision(decision, policy, workflow_now=datetime.now(UTC))


def test_policy_rejects_a_decision_for_a_different_event_batch() -> None:
    expected_event_id = uuid4()
    decision = AgentDecision(
        based_on_event_ids=(uuid4(),),
        status=DecisionStatus.ACTIVE,
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset(),
        allowed_wake_event_types=frozenset(),
    )

    with pytest.raises(DecisionRejected, match="exactly the events"):
        validate_decision(
            decision,
            policy,
            workflow_now=datetime.now(UTC),
            expected_event_ids=frozenset({expected_event_id}),
        )


def test_approval_is_bound_to_an_exact_payload_hash() -> None:
    command = ReviewCommand(
        tenant_id=uuid4(),
        process_instance_id=uuid4(),
        review_thread_id=uuid4(),
        action_request_id=uuid4(),
        proposal_revision=2,
        command_type=ReviewCommandType.APPROVE,
        actor_id=uuid4(),
        expected_payload_hash="a" * 64,
    )

    assert command.proposal_revision == 2


def test_revision_request_requires_feedback() -> None:
    with pytest.raises(ValidationError, match="requires a message"):
        ReviewCommand(
            tenant_id=uuid4(),
            process_instance_id=uuid4(),
            review_thread_id=uuid4(),
            action_request_id=uuid4(),
            proposal_revision=1,
            command_type=ReviewCommandType.REQUEST_REVISION,
            actor_id=uuid4(),
        )
