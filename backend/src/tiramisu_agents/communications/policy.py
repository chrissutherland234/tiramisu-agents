"""Pure deterministic customer-communication policy."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tiramisu_agents.processes.definitions import DailyQuietHours, ProcessDefinition


class CommunicationSafetyBlocked(ValueError):
    """Raised when deterministic policy forbids an outbound action now."""

    def __init__(self, block: "CommunicationBlock") -> None:
        self.block = block
        super().__init__(block.message)


class CommunicationBlockCode(StrEnum):
    PROCESS_EXPIRED = "process_expired"
    OPTED_OUT = "opted_out"
    AUTOMATED_RESPONSE_LOOP = "automated_response_loop"
    QUIET_HOURS = "quiet_hours"
    PROCESS_MESSAGE_LIMIT = "process_message_limit"
    RATE_LIMIT = "rate_limit"
    FOLLOW_UP_LIMIT = "follow_up_limit"
    MINIMUM_INTERVAL = "minimum_interval"


@dataclass(frozen=True, slots=True)
class CommunicationBlock:
    code: CommunicationBlockCode
    message: str
    next_allowed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CommunicationPolicy:
    outbound_action_types: frozenset[str]
    reply_event_types: frozenset[str]
    max_follow_ups_without_reply: int
    minimum_follow_up_interval: timedelta
    opt_out_event_types: frozenset[str] = frozenset()
    automated_response_event_types: frozenset[str] = frozenset()
    quiet_hours: DailyQuietHours | None = None
    max_outbound_messages_per_process: int = 50
    max_outbound_messages_per_window: int = 5
    outbound_message_window: timedelta = timedelta(hours=24)
    maximum_process_lifetime: timedelta = timedelta(days=90)

    @classmethod
    def from_definition(cls, definition: ProcessDefinition) -> "CommunicationPolicy":
        communications = definition.communications
        limits = definition.limits
        return cls(
            outbound_action_types=frozenset(communications.outbound_action_types),
            reply_event_types=frozenset(communications.reply_event_types),
            opt_out_event_types=frozenset(communications.opt_out_event_types),
            automated_response_event_types=frozenset(communications.automated_response_event_types),
            quiet_hours=communications.quiet_hours,
            max_follow_ups_without_reply=limits.max_follow_ups_without_reply,
            minimum_follow_up_interval=timedelta(hours=limits.minimum_follow_up_interval_hours),
            max_outbound_messages_per_process=limits.max_outbound_messages_per_process,
            max_outbound_messages_per_window=limits.max_outbound_messages_per_window,
            outbound_message_window=timedelta(hours=limits.outbound_message_window_hours),
            maximum_process_lifetime=timedelta(days=limits.maximum_process_lifetime_days),
        )


@dataclass(frozen=True, slots=True)
class CommunicationSafetySnapshot:
    evaluated_at: datetime
    outbound_allowed_now: bool
    blocks: tuple[CommunicationBlock, ...]
    outbound_messages_total: int
    outbound_messages_in_window: int
    follow_ups_since_reply: int
    last_human_reply_at: datetime | None
    latest_automated_response_at: datetime | None
    opted_out_at: datetime | None
    process_expires_at: datetime
    rate_window_started_at: datetime

    def require_allowed(self) -> None:
        if self.blocks:
            raise CommunicationSafetyBlocked(self.blocks[0])


@dataclass(frozen=True, slots=True)
class CommunicationSafetyFacts:
    """Integration-neutral evidence used to decide whether contact is allowed."""

    process_created_at: datetime
    outbound_message_times: tuple[datetime, ...] = ()
    prior_outbound_message_times: tuple[datetime, ...] | None = None
    last_human_reply_at: datetime | None = None
    latest_automated_response_at: datetime | None = None
    opted_out_at: datetime | None = None
    reserve_next_message: bool = True


def evaluate_process_lifetime(
    *,
    process_created_at: datetime,
    policy: CommunicationPolicy,
    now: datetime,
) -> CommunicationBlock | None:
    """Return the exact closed-boundary lifetime block, if one applies."""

    now = _as_utc(now, label="process lifetime evaluation time")
    created_at = _as_utc(process_created_at, label="process creation time")
    process_expires_at = created_at + policy.maximum_process_lifetime
    if now < process_expires_at:
        return None
    return CommunicationBlock(
        CommunicationBlockCode.PROCESS_EXPIRED,
        f"process lifetime ended at {process_expires_at.isoformat()}",
    )


def evaluate_communication_safety(
    *,
    policy: CommunicationPolicy,
    facts: CommunicationSafetyFacts,
    now: datetime,
) -> CommunicationSafetySnapshot:
    """Evaluate one send boundary from trusted facts without infrastructure or I/O."""

    now = _as_utc(now, label="communication policy evaluation time")
    process_created_at = _as_utc(facts.process_created_at, label="process creation time")
    outbound_times = tuple(
        _as_utc(value, label="outbound message time") for value in facts.outbound_message_times
    )
    prior_source = (
        facts.outbound_message_times
        if facts.prior_outbound_message_times is None
        else facts.prior_outbound_message_times
    )
    prior_outbound_times = tuple(
        _as_utc(value, label="prior outbound message time") for value in prior_source
    )
    last_human_reply_at = _optional_as_utc(facts.last_human_reply_at, label="human reply time")
    latest_automated_response_at = _optional_as_utc(
        facts.latest_automated_response_at, label="automated response time"
    )
    opted_out_at = _optional_as_utc(facts.opted_out_at, label="opt-out time")

    process_expires_at = process_created_at + policy.maximum_process_lifetime
    window_started_at = now - policy.outbound_message_window
    reservation = int(facts.reserve_next_message)
    total = len(outbound_times)
    in_window = sum(value > window_started_at for value in outbound_times)
    since_reply = sum(
        last_human_reply_at is None or value > last_human_reply_at for value in outbound_times
    )
    latest_prior_at = max(
        (
            value
            for value in prior_outbound_times
            if last_human_reply_at is None or value > last_human_reply_at
        ),
        default=None,
    )

    blocks: list[CommunicationBlock] = []
    lifetime_block = evaluate_process_lifetime(
        process_created_at=process_created_at,
        policy=policy,
        now=now,
    )
    if lifetime_block is not None:
        blocks.append(lifetime_block)
    if opted_out_at is not None:
        blocks.append(
            CommunicationBlock(
                CommunicationBlockCode.OPTED_OUT,
                f"customer opted out at {opted_out_at.isoformat()}",
            )
        )
    if latest_automated_response_at is not None:
        blocks.append(
            CommunicationBlock(
                CommunicationBlockCode.AUTOMATED_RESPONSE_LOOP,
                "latest inbound contact was classified as automated; wait for a human reply",
            )
        )
    quiet_end = _quiet_hours_end(policy.quiet_hours, now)
    if quiet_end is not None:
        blocks.append(
            CommunicationBlock(
                CommunicationBlockCode.QUIET_HOURS,
                f"customer quiet hours are active until {quiet_end.isoformat()}",
                next_allowed_at=quiet_end,
            )
        )
    if total + reservation > policy.max_outbound_messages_per_process:
        blocks.append(
            CommunicationBlock(
                CommunicationBlockCode.PROCESS_MESSAGE_LIMIT,
                "process outbound-message limit has been reached",
            )
        )
    if in_window + reservation > policy.max_outbound_messages_per_window:
        next_allowed_at = (
            min(value for value in outbound_times if value > window_started_at)
            + policy.outbound_message_window
            if in_window
            else None
        )
        blocks.append(
            CommunicationBlock(
                CommunicationBlockCode.RATE_LIMIT,
                "outbound-message rolling-window limit has been reached",
                next_allowed_at=next_allowed_at,
            )
        )
    if since_reply + reservation > policy.max_follow_ups_without_reply:
        blocks.append(
            CommunicationBlock(
                CommunicationBlockCode.FOLLOW_UP_LIMIT,
                "maximum follow-ups without a human reply has been reached",
            )
        )
    if latest_prior_at is not None and now < latest_prior_at + policy.minimum_follow_up_interval:
        next_allowed_at = latest_prior_at + policy.minimum_follow_up_interval
        blocks.append(
            CommunicationBlock(
                CommunicationBlockCode.MINIMUM_INTERVAL,
                f"minimum follow-up interval has not elapsed; retry after "
                f"{next_allowed_at.isoformat()}",
                next_allowed_at=next_allowed_at,
            )
        )
    return CommunicationSafetySnapshot(
        evaluated_at=now,
        outbound_allowed_now=not blocks,
        blocks=tuple(blocks),
        outbound_messages_total=total,
        outbound_messages_in_window=in_window,
        follow_ups_since_reply=since_reply,
        last_human_reply_at=last_human_reply_at,
        latest_automated_response_at=latest_automated_response_at,
        opted_out_at=opted_out_at,
        process_expires_at=process_expires_at,
        rate_window_started_at=window_started_at,
    )


def _quiet_hours_end(configuration: DailyQuietHours | None, now: datetime) -> datetime | None:
    if configuration is None:
        return None
    zone = ZoneInfo(configuration.timezone)
    local_now = now.astimezone(zone)
    overnight = configuration.start_local > configuration.end_local
    for start_date in (local_now.date() - timedelta(days=1), local_now.date()):
        end_date: date = start_date + timedelta(days=int(overnight))
        local_start = datetime.combine(start_date, configuration.start_local)
        local_end = datetime.combine(end_date, configuration.end_local)
        start_utc = _resolve_local_boundary(local_start, zone=zone, earliest=True)
        end_utc = _resolve_local_boundary(local_end, zone=zone, earliest=False)
        if start_utc <= now < end_utc:
            return end_utc
    return None


def _resolve_local_boundary(
    value: datetime,
    *,
    zone: ZoneInfo,
    earliest: bool,
) -> datetime:
    """Resolve DST folds/gaps conservatively around a quiet-hours boundary."""

    candidates = (
        value.replace(tzinfo=zone, fold=0).astimezone(UTC),
        value.replace(tzinfo=zone, fold=1).astimezone(UTC),
    )
    return min(candidates) if earliest else max(candidates)


def _as_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_as_utc(value: datetime | None, *, label: str) -> datetime | None:
    return None if value is None else _as_utc(value, label=label)
