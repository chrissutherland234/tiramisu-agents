"""Integration-free communication-safety boundary tests."""

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from tiramisu_agents.communications import (
    CommunicationBlockCode,
    CommunicationPolicy,
    CommunicationSafetyFacts,
    evaluate_communication_safety,
)
from tiramisu_agents.processes.definitions import DailyQuietHours

NOW = datetime(2026, 6, 15, 12, tzinfo=UTC)


def _policy(**changes: Any) -> CommunicationPolicy:
    values: dict[str, Any] = {
        "outbound_action_types": frozenset({"send_message"}),
        "reply_event_types": frozenset({"customer.replied"}),
        "max_follow_ups_without_reply": 3,
        "minimum_follow_up_interval": timedelta(hours=1),
        "max_outbound_messages_per_process": 50,
        "max_outbound_messages_per_window": 5,
        "outbound_message_window": timedelta(hours=24),
        "maximum_process_lifetime": timedelta(days=90),
    }
    values.update(changes)
    return CommunicationPolicy(**values)


def _facts(**changes: Any) -> CommunicationSafetyFacts:
    values: dict[str, Any] = {"process_created_at": NOW - timedelta(days=1)}
    values.update(changes)
    return CommunicationSafetyFacts(**values)


@pytest.mark.parametrize(
    ("now", "blocked", "expected_end"),
    (
        (
            datetime(2026, 6, 15, 10, tzinfo=UTC),
            True,
            datetime(2026, 6, 15, 11, tzinfo=UTC),
        ),
        (
            datetime(2026, 6, 15, 10, 59, 59, tzinfo=UTC),
            True,
            datetime(2026, 6, 15, 11, tzinfo=UTC),
        ),
        (datetime(2026, 6, 15, 11, tzinfo=UTC), False, None),
        (datetime(2026, 6, 15, 9, 59, 59, tzinfo=UTC), False, None),
    ),
)
def test_same_day_quiet_hours_are_start_inclusive_and_end_exclusive(
    now: datetime,
    blocked: bool,
    expected_end: datetime | None,
) -> None:
    snapshot = evaluate_communication_safety(
        policy=_policy(
            quiet_hours=DailyQuietHours(
                timezone="Pacific/Auckland",
                start_local=time(22),
                end_local=time(23),
            )
        ),
        facts=_facts(),
        now=now,
    )

    quiet = next(
        (block for block in snapshot.blocks if block.code is CommunicationBlockCode.QUIET_HOURS),
        None,
    )
    assert (quiet is not None) is blocked
    assert (None if quiet is None else quiet.next_allowed_at) == expected_end


@pytest.mark.parametrize(
    ("now", "blocked", "expected_end"),
    (
        (
            datetime(2026, 6, 15, 11, tzinfo=UTC),
            True,
            datetime(2026, 6, 15, 19, tzinfo=UTC),
        ),
        (
            datetime(2026, 6, 15, 18, 59, 59, tzinfo=UTC),
            True,
            datetime(2026, 6, 15, 19, tzinfo=UTC),
        ),
        (datetime(2026, 6, 15, 19, tzinfo=UTC), False, None),
        (datetime(2026, 6, 15, 9, 59, 59, tzinfo=UTC), False, None),
    ),
)
def test_overnight_quiet_hours_cross_local_midnight(
    now: datetime,
    blocked: bool,
    expected_end: datetime | None,
) -> None:
    snapshot = evaluate_communication_safety(
        policy=_policy(
            quiet_hours=DailyQuietHours(
                timezone="Pacific/Auckland",
                start_local=time(22),
                end_local=time(7),
            )
        ),
        facts=_facts(),
        now=now,
    )

    quiet = next(
        (block for block in snapshot.blocks if block.code is CommunicationBlockCode.QUIET_HOURS),
        None,
    )
    assert (quiet is not None) is blocked
    assert (None if quiet is None else quiet.next_allowed_at) == expected_end


def test_rolling_window_excludes_its_exact_start_boundary() -> None:
    policy = _policy(max_outbound_messages_per_window=1)

    at_boundary = evaluate_communication_safety(
        policy=policy,
        facts=_facts(outbound_message_times=(NOW - timedelta(hours=24),)),
        now=NOW,
    )
    inside_window = evaluate_communication_safety(
        policy=policy,
        facts=_facts(
            outbound_message_times=(NOW - timedelta(hours=24) + timedelta(microseconds=1),)
        ),
        now=NOW,
    )

    assert at_boundary.outbound_allowed_now is True
    assert at_boundary.outbound_messages_in_window == 0
    rate_block = next(
        block for block in inside_window.blocks if block.code is CommunicationBlockCode.RATE_LIMIT
    )
    assert rate_block.next_allowed_at == NOW + timedelta(microseconds=1)


def test_quiet_hours_choose_the_later_end_during_a_daylight_saving_fold() -> None:
    policy = _policy(
        quiet_hours=DailyQuietHours(
            timezone="Pacific/Auckland",
            start_local=time(1),
            end_local=time(2, 30),
        )
    )

    first_occurrence = evaluate_communication_safety(
        policy=policy,
        facts=CommunicationSafetyFacts(process_created_at=datetime(2026, 4, 3, 13, 30, tzinfo=UTC)),
        now=datetime(2026, 4, 4, 13, 30, tzinfo=UTC),
    )
    second_occurrence = evaluate_communication_safety(
        policy=policy,
        facts=CommunicationSafetyFacts(process_created_at=datetime(2026, 4, 3, 13, 30, tzinfo=UTC)),
        now=datetime(2026, 4, 4, 14, 30, tzinfo=UTC),
    )

    quiet = next(
        block
        for block in first_occurrence.blocks
        if block.code is CommunicationBlockCode.QUIET_HOURS
    )
    assert quiet.next_allowed_at == datetime(2026, 4, 4, 14, 30, tzinfo=UTC)
    assert all(
        block.code is not CommunicationBlockCode.QUIET_HOURS for block in second_occurrence.blocks
    )


def test_quiet_hours_extend_through_a_nonexistent_daylight_saving_end_time() -> None:
    snapshot = evaluate_communication_safety(
        policy=_policy(
            quiet_hours=DailyQuietHours(
                timezone="Pacific/Auckland",
                start_local=time(1),
                end_local=time(2, 30),
            )
        ),
        facts=CommunicationSafetyFacts(
            process_created_at=datetime(2026, 9, 25, 14, 15, tzinfo=UTC)
        ),
        now=datetime(2026, 9, 26, 14, 15, tzinfo=UTC),
    )

    quiet = next(
        block for block in snapshot.blocks if block.code is CommunicationBlockCode.QUIET_HOURS
    )
    assert quiet.next_allowed_at == datetime(2026, 9, 26, 14, 30, tzinfo=UTC)


def test_reply_resets_follow_up_interval_but_not_process_or_window_counts() -> None:
    sent_at = NOW - timedelta(minutes=30)
    snapshot = evaluate_communication_safety(
        policy=_policy(
            max_follow_ups_without_reply=1,
            minimum_follow_up_interval=timedelta(hours=1),
        ),
        facts=_facts(
            outbound_message_times=(sent_at,),
            last_human_reply_at=NOW - timedelta(minutes=5),
        ),
        now=NOW,
    )

    assert snapshot.outbound_allowed_now is True
    assert snapshot.follow_ups_since_reply == 0
    assert snapshot.outbound_messages_total == 1
    assert snapshot.outbound_messages_in_window == 1


def test_all_permanent_and_temporary_blocks_are_reported_in_stable_priority_order() -> None:
    snapshot = evaluate_communication_safety(
        policy=_policy(
            opt_out_event_types=frozenset({"customer.opted_out"}),
            automated_response_event_types=frozenset({"customer.auto_replied"}),
            quiet_hours=DailyQuietHours(timezone="UTC", start_local=time(11), end_local=time(13)),
            max_outbound_messages_per_process=0,
            max_outbound_messages_per_window=0,
            max_follow_ups_without_reply=0,
            maximum_process_lifetime=timedelta(days=1),
        ),
        facts=CommunicationSafetyFacts(
            process_created_at=NOW - timedelta(days=1),
            outbound_message_times=(NOW - timedelta(minutes=30),),
            last_human_reply_at=None,
            latest_automated_response_at=NOW - timedelta(minutes=20),
            opted_out_at=NOW - timedelta(minutes=10),
        ),
        now=NOW,
    )

    assert [block.code for block in snapshot.blocks] == [
        CommunicationBlockCode.PROCESS_EXPIRED,
        CommunicationBlockCode.OPTED_OUT,
        CommunicationBlockCode.AUTOMATED_RESPONSE_LOOP,
        CommunicationBlockCode.QUIET_HOURS,
        CommunicationBlockCode.PROCESS_MESSAGE_LIMIT,
        CommunicationBlockCode.RATE_LIMIT,
        CommunicationBlockCode.FOLLOW_UP_LIMIT,
        CommunicationBlockCode.MINIMUM_INTERVAL,
    ]


def test_evaluator_rejects_naive_trusted_timestamps() -> None:
    with pytest.raises(ValueError, match="process creation time must be timezone-aware"):
        evaluate_communication_safety(
            policy=_policy(),
            facts=CommunicationSafetyFacts(process_created_at=datetime(2026, 1, 1)),
            now=NOW,
        )
