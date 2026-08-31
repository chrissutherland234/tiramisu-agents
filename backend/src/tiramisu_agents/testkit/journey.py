"""Integration-free deterministic driver for the fictional reference journey."""

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from uuid import UUID, uuid4

from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.adapters.stubs.business import StubBusinessState
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.decisions import ActionProposal
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.ports.actions import ProviderActionRequest, ProviderActionResult
from tiramisu_agents.processes.definitions import ProcessDefinition


class ScenarioActionStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    DENIED = "denied"
    SUCCEEDED = "succeeded"


@dataclass(slots=True)
class ScenarioActionRecord:
    proposal: ActionProposal
    permission: PermissionOutcome
    idempotency_key: str
    status: ScenarioActionStatus
    result: ProviderActionResult | None = None


@dataclass(frozen=True, slots=True)
class ReferenceJourneyResult:
    completed: bool
    event_types: tuple[str, ...]
    action_types: tuple[str, ...]
    approval_count: int
    booking_reference: str
    payment_reference: str
    calendar_reference: str


class FictionalJourneyDriver:
    """Exercise process policy and provider ports without infrastructure or a model."""

    def __init__(
        self,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        definition: ProcessDefinition,
        adapters: ActionAdapterRegistry,
        state: StubBusinessState,
    ) -> None:
        self.tenant_id = tenant_id
        self.process_instance_id = process_instance_id
        self.definition = definition
        self.adapters = adapters
        self.state = state
        self.events: list[CanonicalEvent] = []
        self.actions: list[ScenarioActionRecord] = []
        self._actions_by_key: dict[str, ScenarioActionRecord] = {}

    def begin_enquiry(
        self,
        *,
        enquiry_id: str,
        customer_id: str,
        email: str,
        message: str,
    ) -> CanonicalEvent:
        event = CanonicalEvent(
            tenant_id=self.tenant_id,
            process_instance_id=self.process_instance_id,
            event_type="enquiry.created",
            source="stub.website.v1",
            source_event_id=f"enquiry:{enquiry_id}",
            occurred_at=self.state.now,
            external_references=(
                ExternalReference(
                    provider="stub.website.v1",
                    resource_type="enquiry",
                    external_id=enquiry_id,
                ),
                ExternalReference(
                    provider="stub.crm.v1",
                    resource_type="customer",
                    external_id=customer_id,
                ),
            ),
            facts=(
                FactObservation(
                    key="customer.identifier",
                    kind=FactKind.AUTHORITATIVE,
                    value=customer_id,
                ),
                FactObservation(
                    key="customer.email",
                    kind=FactKind.AUTHORITATIVE,
                    value=email,
                ),
                FactObservation(
                    key="customer.initial_request",
                    kind=FactKind.CUSTOMER_CLAIM,
                    value=message,
                ),
            ),
            payload={"customer_id": customer_id, "email": email, "message": message},
        )
        return self.record_event(event)

    def record_event(self, event: CanonicalEvent) -> CanonicalEvent:
        if event.tenant_id != self.tenant_id:
            raise ValueError("scenario event belongs to another tenant")
        if event.process_instance_id != self.process_instance_id:
            raise ValueError("scenario event belongs to another process")
        self.events.append(event)
        return event

    async def submit_action(self, proposal: ActionProposal) -> ScenarioActionRecord:
        existing = self._actions_by_key.get(proposal.logical_action_key)
        if existing is not None:
            if (
                existing.proposal.action_type != proposal.action_type
                or existing.proposal.parameters != proposal.parameters
            ):
                raise ValueError("logical action key was reused for different content")
            return existing
        policy = self.definition.action_policy().evaluate(proposal)
        key = self._idempotency_key(proposal)
        status = {
            PermissionOutcome.ALLOW: ScenarioActionStatus.SUCCEEDED,
            PermissionOutcome.DENY: ScenarioActionStatus.DENIED,
            PermissionOutcome.REQUIRE_APPROVAL: ScenarioActionStatus.PENDING_APPROVAL,
        }[policy.outcome]
        record = ScenarioActionRecord(
            proposal=proposal,
            permission=policy.outcome,
            idempotency_key=key,
            status=status,
        )
        self._actions_by_key[proposal.logical_action_key] = record
        self.actions.append(record)
        if policy.outcome is PermissionOutcome.ALLOW:
            await self._execute(record)
        return record

    async def approve_action(self, logical_action_key: str) -> ScenarioActionRecord:
        try:
            record = self._actions_by_key[logical_action_key]
        except KeyError as error:
            raise LookupError("scenario action not found") from error
        if record.permission is not PermissionOutcome.REQUIRE_APPROVAL:
            raise ValueError("scenario action does not require approval")
        if record.status is ScenarioActionStatus.SUCCEEDED:
            return record
        if record.status is not ScenarioActionStatus.PENDING_APPROVAL:
            raise ValueError("scenario action is not pending approval")
        await self._execute(record)
        return record

    async def _execute(self, record: ScenarioActionRecord) -> None:
        adapter = self.adapters.resolve(record.proposal.action_type)
        record.result = await adapter.execute(
            ProviderActionRequest(
                action_type=record.proposal.action_type,
                parameters=record.proposal.parameters,
                idempotency_key=record.idempotency_key,
            )
        )
        record.status = ScenarioActionStatus.SUCCEEDED

    def _idempotency_key(self, proposal: ActionProposal) -> str:
        identity = {
            "tenant_id": str(self.tenant_id),
            "process_instance_id": str(self.process_instance_id),
            "logical_action_key": proposal.logical_action_key,
            "action_type": proposal.action_type,
            "parameters": proposal.parameters,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode()).hexdigest()


async def run_enquiry_to_completion(
    driver: FictionalJourneyDriver,
) -> ReferenceJourneyResult:
    """Run the public fictional happy path through every provider-neutral primitive."""

    customer_id = "customer-1"
    email = "customer@example.test"
    driver.begin_enquiry(
        enquiry_id="enquiry-1",
        customer_id=customer_id,
        email=email,
        message="I would like to book next week.",
    )

    message = await driver.submit_action(
        ActionProposal(
            logical_action_key="initial_reply",
            action_type="send_message",
            parameters={"recipient": email, "body": "What day works best for you?"},
            rationale="Ask the customer for their preferred day.",
        )
    )
    message = await driver.approve_action(message.proposal.logical_action_key)
    if message.result is None:
        raise RuntimeError("approved message did not execute")
    driver.record_event(
        driver.state.customer_reply(
            tenant_id=driver.tenant_id,
            process_instance_id=driver.process_instance_id,
            message_reference=message.result.provider_reference,
            content="Tuesday morning would be ideal.",
        )
    )

    availability = await driver.submit_action(
        ActionProposal(
            logical_action_key="find_slots",
            action_type="find_available_slots",
            parameters={"days": 7},
            rationale="Find slots matching the customer's request.",
        )
    )
    if availability.result is None:
        raise RuntimeError("availability action did not execute")
    slots = availability.result.result.get("slots")
    if not isinstance(slots, list) or not slots or not isinstance(slots[0], str):
        raise RuntimeError("stub availability returned no usable slots")
    selected_slot = slots[0]

    booking = await driver.submit_action(
        ActionProposal(
            logical_action_key="propose_booking",
            action_type="propose_booking",
            parameters={"customer_id": customer_id, "slot": selected_slot},
            rationale="Offer the first suitable available slot.",
        )
    )
    booking = await driver.approve_action(booking.proposal.logical_action_key)
    if booking.result is None:
        raise RuntimeError("approved booking did not execute")
    booking_reference = booking.result.provider_reference

    payment = await driver.submit_action(
        ActionProposal(
            logical_action_key="request_payment",
            action_type="request_payment",
            parameters={
                "booking_reference": booking_reference,
                "amount_minor": 12_500,
                "currency": "nzd",
            },
            rationale="Request payment for the confirmed booking.",
        )
    )
    payment = await driver.approve_action(payment.proposal.logical_action_key)
    if payment.result is None:
        raise RuntimeError("approved payment request did not execute")
    payment_reference = payment.result.provider_reference
    driver.record_event(
        driver.state.complete_payment(
            tenant_id=driver.tenant_id,
            process_instance_id=driver.process_instance_id,
            payment_reference=payment_reference,
        )
    )

    calendar = await driver.submit_action(
        ActionProposal(
            logical_action_key="create_calendar_event",
            action_type="create_calendar_event",
            parameters={
                "booking_reference": booking_reference,
                "starts_at": selected_slot,
                "title": "Customer booking",
            },
            rationale="Add the paid booking to the calendar.",
        )
    )
    if calendar.result is None:
        raise RuntimeError("calendar action did not execute")

    return ReferenceJourneyResult(
        completed=True,
        event_types=tuple(event.event_type for event in driver.events),
        action_types=tuple(action.proposal.action_type for action in driver.actions),
        approval_count=sum(
            action.permission is PermissionOutcome.REQUIRE_APPROVAL for action in driver.actions
        ),
        booking_reference=booking_reference,
        payment_reference=payment_reference,
        calendar_reference=calendar.result.provider_reference,
    )


def new_scenario_identity() -> tuple[UUID, UUID]:
    """Return isolated tenant/process IDs for integration-free scenarios."""

    return uuid4(), uuid4()
