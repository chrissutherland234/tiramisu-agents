from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tiramisu_agents.core.action_identity import action_payload_identity
from tiramisu_agents.core.contracts.actions import ActionConflict
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    HumanWakeCondition,
    MemoryUpdate,
    TimerWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
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


def test_conflict_facts_must_be_authoritative() -> None:
    with pytest.raises(ValidationError, match="must be authoritative"):
        ActionConflict(
            code="resource_unavailable",
            message="the requested resource is no longer available",
            facts=(
                FactObservation(
                    key="booking.available_slots",
                    kind=FactKind.CUSTOMER_CLAIM,
                    value=["2026-09-02T10:00:00+00:00"],
                ),
            ),
        )


def test_conflict_fact_count_and_complete_payload_are_bounded() -> None:
    fact = FactObservation(
        key="booking.available_slots",
        kind=FactKind.AUTHORITATIVE,
        value="available",
    )
    with pytest.raises(ValidationError, match="action conflict facts exceeds platform limit"):
        ActionConflict(
            code="resource_unavailable",
            message="too many observations",
            facts=(fact,) * 51,
        )

    large_facts = tuple(
        FactObservation(
            key=f"booking.provider_value_{index}",
            kind=FactKind.AUTHORITATIVE,
            value="x" * 16_000,
        )
        for index in range(9)
    )
    with pytest.raises(ValidationError, match="action conflict exceeds platform limit"):
        ActionConflict(
            code="resource_unavailable",
            message="too much combined provider data",
            facts=large_facts,
        )


def test_conflict_message_is_bounded_by_encoded_size() -> None:
    with pytest.raises(ValidationError, match="action conflict message exceeds platform limit"):
        ActionConflict(
            code="resource_unavailable",
            message="😀" * 1_025,
        )


def test_waiting_decision_requires_a_wake_condition() -> None:
    with pytest.raises(ValidationError, match="wake condition"):
        AgentDecision(based_on_event_ids=(), status=DecisionStatus.WAITING)


def test_memory_summary_requires_explicit_source_provenance() -> None:
    with pytest.raises(ValidationError, match="requires source provenance"):
        MemoryUpdate(summary="The customer asked for Tuesday afternoon.")


def test_policy_rejects_memory_provenance_outside_the_turn() -> None:
    event_id = uuid4()
    decision = AgentDecision(
        based_on_event_ids=(event_id,),
        status=DecisionStatus.ACTIVE,
        memory_update=MemoryUpdate(
            summary="The customer asked for Tuesday afternoon.",
            summary_source_event_ids=(uuid4(),),
        ),
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset(),
        allowed_wake_event_types=frozenset(),
    )

    with pytest.raises(DecisionRejected, match="memory summary cites an event outside this turn"):
        validate_decision(
            decision,
            policy,
            workflow_now=datetime.now(UTC),
            expected_event_ids=frozenset({event_id}),
        )


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


def test_policy_rejects_an_unchanged_definitively_conflicted_action() -> None:
    action = ActionProposal(
        logical_action_key="try_same_slot_again",
        action_type="propose_booking",
        parameters={"slot": "2026-09-04T10:00:00+00:00"},
        rationale="Retry the slot without changing anything.",
    )
    decision = AgentDecision(
        based_on_event_ids=(),
        based_on_action_attempt_ids=(uuid4(),),
        status=DecisionStatus.ACTIVE,
        actions=(action,),
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset({"propose_booking"}),
        allowed_wake_event_types=frozenset(),
    )

    with pytest.raises(DecisionRejected, match="just returned a definitive conflict"):
        validate_decision(
            decision,
            policy,
            workflow_now=datetime.now(UTC),
            conflicted_action_payload_hashes=frozenset(
                {action_payload_identity(action.action_type, action.parameters)}
            ),
        )


def test_policy_rejects_an_active_decision_without_a_progress_path() -> None:
    decision = AgentDecision(based_on_event_ids=(), status=DecisionStatus.ACTIVE)
    policy = DecisionPolicy(
        allowed_action_types=frozenset(),
        allowed_wake_event_types=frozenset(),
    )

    with pytest.raises(DecisionRejected, match="requires an action or wake condition"):
        validate_decision(decision, policy, workflow_now=datetime.now(UTC))


def test_policy_rejects_orphaned_human_wake_condition() -> None:
    decision = AgentDecision(
        based_on_event_ids=(),
        status=DecisionStatus.WAITING,
        wake_conditions=(HumanWakeCondition(interaction="approval"),),
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset({"send_message"}),
        allowed_wake_event_types=frozenset(),
        human_wake_action_types=frozenset({"send_message"}),
    )

    with pytest.raises(DecisionRejected, match="requires an action"):
        validate_decision(decision, policy, workflow_now=datetime.now(UTC))


def test_policy_accepts_human_wake_for_an_approval_action() -> None:
    decision = AgentDecision(
        based_on_event_ids=(),
        status=DecisionStatus.WAITING,
        actions=(
            ActionProposal(
                logical_action_key="send_follow_up",
                action_type="send_message",
                rationale="The customer needs a follow-up.",
            ),
        ),
        wake_conditions=(HumanWakeCondition(interaction="approval"),),
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset({"send_message"}),
        allowed_wake_event_types=frozenset(),
        human_wake_action_types=frozenset({"send_message"}),
    )

    assert validate_decision(decision, policy, workflow_now=datetime.now(UTC)) is decision


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


def test_policy_rejects_a_decision_for_a_different_action_result_batch() -> None:
    expected_attempt_id = uuid4()
    decision = AgentDecision(
        based_on_event_ids=(),
        based_on_action_attempt_ids=(uuid4(),),
        status=DecisionStatus.ACTIVE,
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset(),
        allowed_wake_event_types=frozenset(),
    )

    with pytest.raises(DecisionRejected, match="exactly the action results"):
        validate_decision(
            decision,
            policy,
            workflow_now=datetime.now(UTC),
            expected_action_attempt_ids=frozenset({expected_attempt_id}),
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
