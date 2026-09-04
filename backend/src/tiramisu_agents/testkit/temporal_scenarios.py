"""Cross-layer executable scenarios using PostgreSQL and a real Temporal mailbox."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tiramisu_agents.actions.execution import ActionExecutor
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.agents.context import PostgresAgentContextLoader
from tiramisu_agents.agents.runner import ProposalCorrection
from tiramisu_agents.core.contracts.actions import (
    ActionAttemptStatus,
    PermissionOutcome,
)
from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.contracts.events import ExternalReference
from tiramisu_agents.core.contracts.processes import (
    AgentTurnInput,
    ProcessSnapshot,
    ProcessStatus,
)
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionReconciliationDecision,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import (
    EventInbox,
    ExternalCorrelation,
    OutboxMessage,
    OutboxRecoveryCommand,
)
from tiramisu_agents.db.models.processes import (
    ProcessControlCommand,
    ProcessInstance,
    ProcessIntervention,
    ProcessStateRevision,
)
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import (
    Tenant,
    TenantCredential,
    TenantDeploymentEvent,
    TenantSafetyEvent,
)
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService
from tiramisu_agents.extensions import ClientPack
from tiramisu_agents.extensions.project_metadata import ScenarioStepDescription
from tiramisu_agents.extensions.runtime import DeploymentRelease
from tiramisu_agents.projects.contracts import (
    ScenarioAction,
    ScenarioEventWait,
    ScenarioTimerWait,
)
from tiramisu_agents.reviews.service import ReviewService
from tiramisu_agents.temporal.activities.action_execution import ActionExecutionActivities
from tiramisu_agents.temporal.activities.action_gateway import ActionGatewayActivities
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities
from tiramisu_agents.temporal.activities.process_state import ProcessStateActivities
from tiramisu_agents.temporal.dispatcher import DispatchStatus, TemporalOutboxDispatcher
from tiramisu_agents.temporal.workflows.mailbox import MailboxState, ProcessMailboxWorkflow
from tiramisu_agents.testkit.deployment import make_test_deployment_release
from tiramisu_agents.testkit.scenario_script import CompiledScenarioScript, ScenarioRunError
from tiramisu_agents.testkit.scenarios import (
    ScenarioResult,
    ScenarioTraceEntry,
    ScenarioTraceKind,
)


class _CheckpointKind(StrEnum):
    APPROVAL = "approval"
    EVENT = "event"
    TIMER = "timer"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class _ScenarioCheckpoint:
    kind: _CheckpointKind
    step_index: int
    step: ScenarioStepDescription
    action_request_id: UUID | None = None
    event_step_index: int | None = None
    delay: timedelta | None = None


@dataclass(frozen=True, slots=True)
class _ExpectedWake:
    kind: str
    step: ScenarioStepDescription
    action_request_id: UUID | None = None


class _CompiledScenarioAgent:
    """Agent-turn runner whose only decisions come from compiled scenario steps."""

    def __init__(self, script: CompiledScenarioScript) -> None:
        self.script = script
        self.cursor = 0
        self.expected_wake: _ExpectedWake | None = None
        self.checkpoint: _ScenarioCheckpoint | None = None
        self.failure: ScenarioRunError | None = None
        self.trace: list[ScenarioTraceEntry] = []
        self.turn_inputs: list[AgentTurnInput] = []
        self._decisions_by_turn: dict[UUID, AgentDecision] = {}

    async def run_turn(
        self,
        turn_input: AgentTurnInput,
        *,
        correction: ProposalCorrection | None = None,
    ) -> AgentDecision:
        cached = self._decisions_by_turn.get(turn_input.turn_id)
        if cached is not None and correction is None:
            return cached
        try:
            if correction is not None:
                step = self.current_step()
                raise self.script.error(
                    step,
                    "production validation rejected the compiled decision: "
                    f"{correction.validation_error}",
                )
            self.turn_inputs.append(turn_input)
            if turn_input.workflow_now is None:
                raise ScenarioRunError("Temporal scenario agent requires the workflow clock")
            decision = self._next_decision(turn_input)
            self._decisions_by_turn[turn_input.turn_id] = decision
            return decision
        except ScenarioRunError as error:
            self.failure = error
            raise
        except Exception as error:
            step = self.current_step()
            wrapped = self.script.error(
                step,
                f"scenario agent failed with {type(error).__name__}: {error}",
            )
            self.failure = wrapped
            raise wrapped from error

    def acknowledge(self, checkpoint: _ScenarioCheckpoint) -> None:
        if self.checkpoint != checkpoint:
            raise ScenarioRunError("scenario driver tried to acknowledge a stale checkpoint")
        self.checkpoint = None

    @property
    def complete(self) -> bool:
        return self.cursor == len(self.script.scenario.steps)

    def _next_decision(self, turn_input: AgentTurnInput) -> AgentDecision:
        assert turn_input.workflow_now is not None
        now = turn_input.workflow_now
        self._consume_wake(turn_input, now=now)
        snapshot = self.script.prospective_snapshot(turn_input)
        assertions: list[tuple[ScenarioStepDescription, Any]] = []
        while self.cursor < len(self.script.scenario.steps):
            candidate = self.script.scenario.steps[self.cursor]
            if candidate.kind != "fact":
                break
            assertions.append((candidate, self.script.assert_fact_value(candidate, snapshot)))
            self.cursor += 1
        step = self.current_step()
        if step.kind not in {"action", "wait", "complete"}:
            raise self.script.error(step, "expected an action, wait, or completion decision")
        step_index = self.cursor
        decision = self.script.build_decision(
            step=step,
            step_index=step_index,
            turn_input=turn_input,
            now=now,
        )
        self.append_trace(
            now,
            ScenarioTraceKind.DECISION,
            step.description,
            {
                "status": decision.status.value,
                "actions": [action.action_type for action in decision.actions],
                "wake_conditions": [
                    wake.model_dump(mode="json") for wake in decision.wake_conditions
                ],
            },
        )
        for assertion, actual in assertions:
            self.append_trace(
                now,
                ScenarioTraceKind.FACT,
                assertion.description,
                {"fact_key": assertion.reference, "value": actual},
            )
        self.cursor += 1

        if step.kind == "action":
            action_spec = ScenarioAction.model_validate(step.value)
            expected_parameters = cast(
                dict[str, Any],
                self.script.resolve(action_spec.parameters, snapshot, step=step),
            )
            self.script.require_expected_action(
                step,
                decision,
                action_spec,
                expected_parameters=expected_parameters,
            )
            action = decision.actions[0]
            permission = self.script.definition.action_policy().evaluate(action).outcome
            if permission is PermissionOutcome.DENY:
                raise self.script.error(step, f"policy denied {action.action_type}")
            self.expected_wake = _ExpectedWake(
                kind="action_result",
                step=step,
                action_request_id=action.action_request_id,
            )
            self.append_trace(
                now,
                ScenarioTraceKind.ACTION,
                f"Proposed {action.action_type}",
                {
                    "permission": permission.value,
                    "logical_action_key": action.logical_action_key,
                    "action_request_id": str(action.action_request_id),
                },
            )
            if permission is PermissionOutcome.REQUIRE_APPROVAL:
                self.checkpoint = _ScenarioCheckpoint(
                    kind=_CheckpointKind.APPROVAL,
                    step_index=step_index,
                    step=step,
                    action_request_id=action.action_request_id,
                )
        elif step.kind == "wait":
            self.script.require_expected_wait(step, decision, now=now)
            wait = self.script.parse_wait(step)
            self.append_trace(
                now,
                ScenarioTraceKind.WAKE,
                step.description,
                wait.model_dump(mode="json"),
            )
            if isinstance(wait, ScenarioEventWait):
                event_step_index = self.cursor
                if event_step_index >= len(self.script.scenario.steps):
                    raise self.script.error(step, "event wait has no following event")
                event_step = self.script.scenario.steps[event_step_index]
                if event_step.kind != "event" or event_step.reference != wait.event_type:
                    raise self.script.error(step, "event wait is not followed by its event")
                self.expected_wake = _ExpectedWake(kind="event", step=event_step)
                self.checkpoint = _ScenarioCheckpoint(
                    kind=_CheckpointKind.EVENT,
                    step_index=step_index,
                    step=step,
                    event_step_index=event_step_index,
                )
            else:
                self.expected_wake = _ExpectedWake(kind="timer", step=step)
                self.checkpoint = _ScenarioCheckpoint(
                    kind=_CheckpointKind.TIMER,
                    step_index=step_index,
                    step=step,
                    delay=timedelta(seconds=wait.delay_seconds),
                )
        else:
            self.expected_wake = None
            self.checkpoint = _ScenarioCheckpoint(
                kind=_CheckpointKind.COMPLETE,
                step_index=step_index,
                step=step,
            )
            self.append_trace(
                now,
                ScenarioTraceKind.COMPLETE,
                step.description,
                {"status": ProcessStatus.COMPLETED.value},
            )
        return decision

    def _consume_wake(self, turn_input: AgentTurnInput, *, now: datetime) -> None:
        expected = self.expected_wake
        if expected is None:
            step = self.current_step()
            if step.kind != "event":
                raise self.script.error(step, "agent turn has no scripted wake source")
            self._require_only(turn_input, step=step, events=1)
            event = turn_input.events[0]
            if event.event_type != step.reference:
                raise self.script.error(
                    step,
                    f"received event {event.event_type}; expected {step.reference}",
                )
            self.append_trace(
                event.occurred_at,
                ScenarioTraceKind.EVENT,
                step.description,
                {"event_type": event.event_type, "event_id": str(event.event_id)},
            )
            self.cursor += 1
            return
        if expected.kind == "event":
            self._require_only(turn_input, step=expected.step, events=1)
            event = turn_input.events[0]
            if event.event_type != expected.step.reference:
                raise self.script.error(
                    expected.step,
                    f"received event {event.event_type}; expected {expected.step.reference}",
                )
            self.append_trace(
                event.occurred_at,
                ScenarioTraceKind.EVENT,
                expected.step.description,
                {"event_type": event.event_type, "event_id": str(event.event_id)},
            )
            self.cursor += 1
        elif expected.kind == "action_result":
            self._require_only(turn_input, step=expected.step, action_results=1)
            result = turn_input.action_results[0]
            if (
                result.action_request_id != expected.action_request_id
                or result.action_type != expected.step.reference
                or result.status is not ActionAttemptStatus.SUCCEEDED
            ):
                raise self.script.error(
                    expected.step,
                    "durable action result does not match the scripted successful action",
                )
            self.append_trace(
                now,
                ScenarioTraceKind.RESULT,
                f"{result.action_type} succeeded",
                {
                    "action_request_id": str(result.action_request_id),
                    "attempt_id": str(result.attempt_id),
                    "provider_reference": result.provider_reference,
                },
            )
        elif expected.kind == "timer":
            self._require_only(turn_input, step=expected.step, timers=1)
        else:
            raise self.script.error(expected.step, f"unsupported wake kind {expected.kind}")
        self.expected_wake = None

    def current_step(self) -> ScenarioStepDescription:
        if self.cursor >= len(self.script.scenario.steps):
            return self.script.scenario.steps[-1]
        return self.script.scenario.steps[self.cursor]

    def _require_only(
        self,
        turn_input: AgentTurnInput,
        *,
        step: ScenarioStepDescription,
        events: int = 0,
        action_results: int = 0,
        timers: int = 0,
    ) -> None:
        counts = (
            len(turn_input.events),
            len(turn_input.reviews),
            len(turn_input.action_results),
            len(turn_input.timer_ids),
        )
        expected = (events, 0, action_results, timers)
        if counts != expected:
            raise self.script.error(
                step,
                "turn wake sources differ from the compiled script: "
                f"received {counts}, expected {expected}",
            )

    def append_trace(
        self,
        occurred_at: datetime,
        kind: ScenarioTraceKind,
        description: str,
        details: dict[str, Any],
    ) -> None:
        self.trace.append(
            ScenarioTraceEntry(
                sequence=len(self.trace) + 1,
                occurred_at=occurred_at,
                kind=kind,
                description=description,
                details=details,
            )
        )


class PostgresTemporalScenarioDriver:
    """Run a compiled scenario through PostgreSQL, Temporal, and safe adapters.

    The driver owns an isolated tenant and intentionally restarts its worker at each
    external checkpoint. It is an integration-test runtime: all action execution is
    bound exclusively to adapters explicitly marked safe in ``simulation_bindings``.
    """

    def __init__(
        self,
        client_pack: ClientPack,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        admin_session_factory: async_sessionmaker[AsyncSession],
        environment: WorkflowEnvironment,
        deployment_release: DeploymentRelease | None = None,
        cleanup: bool = True,
        checkpoint_timeout: float = 15.0,
    ) -> None:
        if client_pack.project is None:
            raise ScenarioRunError("client pack has no conventional project scenarios")
        if any(
            getattr(adapter, "is_simulation_adapter", False) is not True
            for adapter in client_pack.simulation_bindings.values()
        ):
            raise ScenarioRunError("Temporal scenarios require explicitly safe adapters")
        self._pack = client_pack
        self._sessions = session_factory
        self._admin_sessions = admin_session_factory
        self._environment = environment
        self._release = deployment_release or make_test_deployment_release(
            client_pack_fingerprint=client_pack.fingerprint(),
            deployment_id="scenario-deployment",
            build_id=f"scenario-driver-{uuid4()}",
        )
        self._release.require_client_pack(client_pack.fingerprint())
        self._cleanup = cleanup
        self._checkpoint_timeout = checkpoint_timeout
        self._run_lock = asyncio.Lock()
        self.last_tenant_id: UUID | None = None
        self.last_process_instance_id: UUID | None = None
        self.worker_start_count = 0

    async def run(self, scenario_id: str) -> ScenarioResult:
        async with self._run_lock:
            return await self._run_isolated(scenario_id)

    async def _run_isolated(self, scenario_id: str) -> ScenarioResult:
        tenant_id = uuid4()
        actor_id = uuid4()
        base = CompiledScenarioScript(self._pack, scenario_id)
        script = CompiledScenarioScript(
            self._pack,
            scenario_id,
            run_identity=f"{base.identity}:postgres-temporal:{tenant_id}",
        )
        agent = _CompiledScenarioAgent(script)
        self.last_tenant_id = tenant_id
        self.last_process_instance_id = None
        self.worker_start_count = 0
        workflow_result: MailboxState | None = None
        ingested_event_ids: list[UUID] = []
        process_id: UUID | None = None
        try:
            await self._create_tenant(tenant_id)
            process_id, initial_event_id = await self._start_process(script, tenant_id)
            self.last_process_instance_id = process_id
            ingested_event_ids.append(initial_event_id)
            workflow_id = f"tenant/{tenant_id}/process/{process_id}"
            handle = self._environment.client.get_workflow_handle_for(
                ProcessMailboxWorkflow.run,
                workflow_id,
            )
            for _ in range(max(10, len(script.scenario.steps) * 3)):
                async with self._worker(agent, tenant_id):
                    await self._dispatch_all(tenant_id)
                    checkpoint = await self._wait_for_checkpoint(
                        agent,
                        handle,
                        tenant_id=tenant_id,
                        process_instance_id=process_id,
                    )
                if checkpoint.kind is _CheckpointKind.APPROVAL:
                    await self._approve(
                        script,
                        agent,
                        checkpoint,
                        tenant_id=tenant_id,
                        process_instance_id=process_id,
                        actor_id=actor_id,
                    )
                    agent.acknowledge(checkpoint)
                elif checkpoint.kind is _CheckpointKind.EVENT:
                    event_id = await self._send_event(
                        script,
                        checkpoint,
                        tenant_id=tenant_id,
                        process_instance_id=process_id,
                    )
                    ingested_event_ids.append(event_id)
                    agent.acknowledge(checkpoint)
                elif checkpoint.kind is _CheckpointKind.TIMER:
                    agent.acknowledge(checkpoint)
                    assert checkpoint.delay is not None
                    async with self._worker(agent, tenant_id):
                        await self._environment.sleep(checkpoint.delay)
                        await self._wait_until_checkpoint_changes(
                            agent,
                            handle,
                            tenant_id=tenant_id,
                            process_instance_id=process_id,
                        )
                else:
                    async with self._worker(agent, tenant_id):
                        workflow_result = await asyncio.wait_for(
                            handle.result(), timeout=self._checkpoint_timeout
                        )
                    break
            else:
                raise ScenarioRunError(
                    f"scenario {scenario_id} exceeded its cross-layer checkpoint limit"
                )
            return await self._validate_result(
                script,
                agent,
                workflow_result,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                actor_id=actor_id,
                ingested_event_ids=tuple(ingested_event_ids),
            )
        finally:
            if self._cleanup:
                await self._delete_tenant_data(tenant_id)

    async def _create_tenant(self, tenant_id: UUID) -> None:
        async with self._admin_sessions.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"scenario-{tenant_id}",
                    name="Executable Scenario Tenant",
                    deployment_id=self._release.deployment_id,
                )
            )

    async def _start_process(
        self,
        script: CompiledScenarioScript,
        tenant_id: UUID,
    ) -> tuple[UUID, UUID]:
        step = script.scenario.steps[0]
        placeholder = ProcessSnapshot(
            tenant_id=tenant_id,
            process_instance_id=script.deterministic_uuid("unstarted-process"),
            process_type=script.journey.id,
            process_definition_version=script.journey.version,
            status=ProcessStatus.ACTIVE,
        )
        event = script.build_event(
            step=step,
            step_index=0,
            tenant_id=tenant_id,
            process_id=None,
            occurred_at=script.scenario.started_at,
            snapshot=placeholder,
        )
        if not event.external_references:
            event = event.model_copy(
                update={
                    "external_references": (
                        ExternalReference(
                            provider="tiramisu.scenario",
                            resource_type=script.journey.id,
                            external_id=str(script.deterministic_uuid("bootstrap-reference")),
                        ),
                    )
                }
            )
        try:
            bootstrap = self._pack.trigger_rules(self._release)[event.event_type]
        except KeyError as error:
            raise script.error(step, "start event has no published trigger rule") from error
        async with self._sessions.begin() as session:
            result = await EventIngestionService().ingest(
                session,
                event,
                bootstrap=bootstrap,
                deployment_id=self._release.deployment_id,
            )
        if result.process_instance_id is None or result.correlation_status != "matched":
            raise script.error(step, "start event did not create a matched process")
        return result.process_instance_id, event.event_id

    async def _send_event(
        self,
        script: CompiledScenarioScript,
        checkpoint: _ScenarioCheckpoint,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
    ) -> UUID:
        assert checkpoint.event_step_index is not None
        step = script.scenario.steps[checkpoint.event_step_index]
        snapshot = await self._load_snapshot(tenant_id, process_instance_id)
        occurred_at = await self._environment.get_current_time()
        event = script.build_event(
            step=step,
            step_index=checkpoint.event_step_index,
            tenant_id=tenant_id,
            process_id=process_instance_id,
            occurred_at=occurred_at,
            snapshot=snapshot,
        )
        async with self._sessions.begin() as session:
            result = await EventIngestionService().ingest(
                session,
                event,
                deployment_id=self._release.deployment_id,
            )
        if (
            result.process_instance_id != process_instance_id
            or result.correlation_status != "matched"
            or result.outbox_message_id is None
        ):
            raise script.error(step, "event did not enter the matched ingestion/outbox path")
        return event.event_id

    @asynccontextmanager
    async def _worker(
        self,
        agent: _CompiledScenarioAgent,
        tenant_id: UUID,
    ) -> AsyncGenerator[None]:
        authorized = frozenset({tenant_id})
        turn_activities = AgentTurnActivities(
            self._sessions,
            self._pack.registry,
            agent,
            compatibility=self._pack.compatibility,
            deployment_release=self._release,
            context_loader=PostgresAgentContextLoader(),
            authorized_tenant_ids=authorized,
        )
        gateway_activities = ActionGatewayActivities(
            self._sessions,
            self._pack.registry,
            deployment_release=self._release,
            authorized_tenant_ids=authorized,
        )
        state_activities = ProcessStateActivities(
            self._sessions,
            self._pack.registry,
            deployment_release=self._release,
            authorized_tenant_ids=authorized,
        )
        execution_activities = ActionExecutionActivities(
            ActionExecutor(
                self._sessions,
                ActionAdapterRegistry(self._pack.simulation_bindings),
                self._pack.compatibility,
                self._release,
            ),
            authorized_tenant_ids=authorized,
        )
        self.worker_start_count += 1
        async with Worker(
            self._environment.client,
            task_queue=self._release.temporal_task_queue,
            workflows=[ProcessMailboxWorkflow],
            activities=[
                turn_activities.run_agent_turn,
                gateway_activities.persist_agent_actions,
                state_activities.persist_process_state,
                state_activities.record_process_intervention,
                execution_activities.execute_action,
                execution_activities.reconcile_action,
            ],
            max_cached_workflows=0,
        ):
            yield

    async def _dispatch_all(self, tenant_id: UUID) -> None:
        dispatcher = TemporalOutboxDispatcher(
            self._sessions,
            self._environment.client,
            deployment_release=self._release,
            authorized_tenant_ids=frozenset({tenant_id}),
            orchestrate_agent_turns=True,
        )
        for _ in range(100):
            result = await dispatcher.dispatch_one(tenant_id)
            if result.status is DispatchStatus.EMPTY:
                return
            if result.status is not DispatchStatus.PUBLISHED:
                raise ScenarioRunError(
                    f"scenario outbox delivery failed: {result.status.value}: {result.error}"
                )
        raise ScenarioRunError("scenario outbox did not drain after 100 deliveries")

    async def _wait_for_checkpoint(
        self,
        agent: _CompiledScenarioAgent,
        handle: Any,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
    ) -> _ScenarioCheckpoint:
        deadline = asyncio.get_running_loop().time() + self._checkpoint_timeout
        while asyncio.get_running_loop().time() < deadline:
            if agent.failure is not None:
                raise agent.failure
            checkpoint = agent.checkpoint
            process, intervention = await self._load_process_and_intervention(
                tenant_id,
                process_instance_id,
            )
            if intervention is not None:
                raise ScenarioRunError(
                    f"scenario runtime opened {intervention.kind}: "
                    f"{intervention.error_type}: {intervention.error}"
                )
            if checkpoint is not None:
                state = cast(MailboxState, await handle.query(ProcessMailboxWorkflow.state))
                if not state.turn_in_progress and await self._checkpoint_is_durable(
                    checkpoint,
                    process,
                    state,
                    tenant_id=tenant_id,
                    process_instance_id=process_instance_id,
                ):
                    return checkpoint
            await asyncio.sleep(0.02)
        step = agent.current_step()
        raise agent.script.error(
            step,
            "runtime did not reach the next durable scenario checkpoint before timeout",
        )

    async def _wait_until_checkpoint_changes(
        self,
        agent: _CompiledScenarioAgent,
        handle: Any,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self._checkpoint_timeout
        while asyncio.get_running_loop().time() < deadline:
            if agent.failure is not None:
                raise agent.failure
            if agent.checkpoint is not None:
                return
            _, intervention = await self._load_process_and_intervention(
                tenant_id,
                process_instance_id,
            )
            if intervention is not None:
                raise ScenarioRunError(
                    f"scenario timer wake opened {intervention.kind}: {intervention.error}"
                )
            state = cast(MailboxState, await handle.query(ProcessMailboxWorkflow.state))
            if state.closed:
                return
            await asyncio.sleep(0.02)
        raise agent.script.error(
            agent.current_step(),
            "Temporal timer did not wake the scenario before timeout",
        )

    async def _checkpoint_is_durable(
        self,
        checkpoint: _ScenarioCheckpoint,
        process: ProcessInstance,
        state: MailboxState,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
    ) -> bool:
        if checkpoint.kind is _CheckpointKind.APPROVAL:
            approval = await self._pending_approval(
                tenant_id,
                process_instance_id,
                cast(UUID, checkpoint.action_request_id),
            )
            return (
                process.status == ProcessStatus.REVIEW.value
                and approval is not None
                and state.wake_plan is not None
                and state.wake_plan.human_interactions == ("approval",)
            )
        if checkpoint.kind is _CheckpointKind.EVENT:
            wait = ScenarioEventWait.model_validate(checkpoint.step.value)
            db_events = tuple(
                item["event_type"]
                for item in process.current_wake_conditions
                if item["type"] == "event"
            )
            return (
                process.status == ProcessStatus.WAITING.value
                and db_events == (wait.event_type,)
                and state.wake_plan is not None
                and state.wake_plan.event_types == (wait.event_type,)
            )
        if checkpoint.kind is _CheckpointKind.TIMER:
            db_timers = tuple(
                item for item in process.current_wake_conditions if item["type"] == "timer"
            )
            return (
                process.status == ProcessStatus.WAITING.value
                and len(db_timers) == 1
                and state.wake_plan is not None
                and state.wake_plan.timer_id is not None
                and state.wake_plan.timer_at is not None
            )
        return process.status == ProcessStatus.COMPLETED.value and state.closed

    async def _approve(
        self,
        script: CompiledScenarioScript,
        agent: _CompiledScenarioAgent,
        checkpoint: _ScenarioCheckpoint,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        actor_id: UUID,
    ) -> None:
        action_request_id = cast(UUID, checkpoint.action_request_id)
        async with self._sessions.begin() as session:
            await set_tenant_context(session, tenant_id)
            row = (
                await session.execute(
                    select(ReviewThread, ApprovalRequest, ActionRequest)
                    .join(
                        ApprovalRequest,
                        ApprovalRequest.id == ReviewThread.approval_request_id,
                    )
                    .join(ActionRequest, ActionRequest.id == ApprovalRequest.action_request_id)
                    .where(
                        ReviewThread.process_instance_id == process_instance_id,
                        ReviewThread.status == "open",
                        ApprovalRequest.action_request_id == action_request_id,
                        ApprovalRequest.status == "pending",
                    )
                )
            ).one_or_none()
            if row is None:
                raise script.error(checkpoint.step, "expected exact-payload approval is absent")
            thread, approval, action = row
            await ReviewService().apply(
                session,
                ReviewCommand(
                    command_id=script.deterministic_uuid(
                        f"approval:{checkpoint.step_index}:{action_request_id}"
                    ),
                    tenant_id=tenant_id,
                    process_instance_id=process_instance_id,
                    review_thread_id=thread.id,
                    action_request_id=action_request_id,
                    proposal_revision=approval.revision,
                    command_type=ReviewCommandType.APPROVE,
                    actor_id=actor_id,
                    expected_payload_hash=approval.payload_hash,
                    message=f"Scenario approval: {checkpoint.step.description}",
                ),
            )
        occurred_at = await self._environment.get_current_time()
        agent.append_trace(
            occurred_at,
            ScenarioTraceKind.APPROVAL,
            f"Approved {action.action_type}",
            {
                "logical_action_key": action.logical_action_key,
                "action_request_id": str(action_request_id),
                "payload_hash": approval.payload_hash,
            },
        )

    async def _pending_approval(
        self,
        tenant_id: UUID,
        process_instance_id: UUID,
        action_request_id: UUID,
    ) -> ApprovalRequest | None:
        async with self._sessions.begin() as session:
            await set_tenant_context(session, tenant_id)
            return await session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.process_instance_id == process_instance_id,
                    ApprovalRequest.action_request_id == action_request_id,
                    ApprovalRequest.status == "pending",
                )
            )

    async def _load_snapshot(
        self,
        tenant_id: UUID,
        process_instance_id: UUID,
    ) -> ProcessSnapshot:
        async with self._sessions.begin() as session:
            await set_tenant_context(session, tenant_id)
            process = await session.get(ProcessInstance, process_instance_id)
            if process is None:
                raise ScenarioRunError("scenario process disappeared")
            return ProcessSnapshot(
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                process_type=process.process_type,
                process_definition_version=process.definition_version,
                status=ProcessStatus(process.status),
                authoritative_facts=dict(process.authoritative_facts),
                customer_claims=dict(process.customer_claims),
                fact_provenance=dict(process.fact_provenance),
                memory_summary=process.memory_summary,
                memory_summary_source_event_ids=tuple(
                    UUID(value) for value in process.memory_summary_source_event_ids
                ),
                memory_summary_source_review_command_ids=tuple(
                    UUID(value) for value in process.memory_summary_source_review_command_ids
                ),
                memory_summary_source_action_attempt_ids=tuple(
                    UUID(value) for value in process.memory_summary_source_action_attempt_ids
                ),
                memory_summary_source_timer_ids=tuple(process.memory_summary_source_timer_ids),
                open_commitments=tuple(process.open_commitments),
                state_version=process.state_version,
            )

    async def _load_process_and_intervention(
        self,
        tenant_id: UUID,
        process_instance_id: UUID,
    ) -> tuple[ProcessInstance, ProcessIntervention | None]:
        async with self._sessions.begin() as session:
            await set_tenant_context(session, tenant_id)
            process = await session.get(ProcessInstance, process_instance_id)
            intervention = await session.scalar(
                select(ProcessIntervention)
                .where(
                    ProcessIntervention.process_instance_id == process_instance_id,
                    ProcessIntervention.status == "open",
                )
                .order_by(ProcessIntervention.created_at.desc())
                .limit(1)
            )
            if process is None:
                raise ScenarioRunError("scenario process disappeared")
            return process, intervention

    async def _validate_result(
        self,
        script: CompiledScenarioScript,
        agent: _CompiledScenarioAgent,
        workflow_result: MailboxState,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        actor_id: UUID,
        ingested_event_ids: tuple[UUID, ...],
    ) -> ScenarioResult:
        if agent.failure is not None:
            raise agent.failure
        if not agent.complete:
            raise ScenarioRunError(
                f"scenario {script.scenario.id} completed before all compiled steps were consumed"
            )
        expected_action_steps = [
            (index, step, ScenarioAction.model_validate(step.value))
            for index, step in enumerate(script.scenario.steps)
            if step.kind == "action"
        ]
        expected_action_ids = tuple(
            script.deterministic_uuid(f"action:{index}:0:{action.logical_action_key}")
            for index, _, action in expected_action_steps
        )
        expected_approval_ids = {
            action_id
            for action_id, (_, _, action) in zip(
                expected_action_ids,
                expected_action_steps,
                strict=True,
            )
            if action.approve
        }
        async with self._sessions.begin() as session:
            await set_tenant_context(session, tenant_id)
            process = await session.get(ProcessInstance, process_instance_id)
            inbox = (
                await session.scalars(
                    select(EventInbox).where(EventInbox.process_instance_id == process_instance_id)
                )
            ).all()
            outbox = (
                await session.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.process_instance_id == process_instance_id
                    )
                )
            ).all()
            actions = (
                await session.scalars(
                    select(ActionRequest).where(
                        ActionRequest.process_instance_id == process_instance_id
                    )
                )
            ).all()
            attempts = (
                await session.scalars(
                    select(ActionAttempt).where(
                        ActionAttempt.process_instance_id == process_instance_id
                    )
                )
            ).all()
            approvals = (
                await session.execute(
                    select(ApprovalRequest, ApprovalDecision)
                    .join(
                        ApprovalDecision,
                        ApprovalDecision.approval_request_id == ApprovalRequest.id,
                    )
                    .where(ApprovalRequest.process_instance_id == process_instance_id)
                )
            ).all()
            review_messages = (
                await session.scalars(
                    select(ReviewMessage).where(
                        ReviewMessage.process_instance_id == process_instance_id
                    )
                )
            ).all()
            revisions = (
                await session.scalars(
                    select(ProcessStateRevision).where(
                        ProcessStateRevision.process_instance_id == process_instance_id
                    )
                )
            ).all()
            interventions = (
                await session.scalars(
                    select(ProcessIntervention).where(
                        ProcessIntervention.process_instance_id == process_instance_id
                    )
                )
            ).all()
        if process is None:
            raise ScenarioRunError("scenario process is unavailable at completion")
        expected_event_steps = [step for step in script.scenario.steps if step.kind == "event"]
        if (
            process.status != ProcessStatus.COMPLETED.value
            or process.current_wake_conditions != []
            or not workflow_result.closed
        ):
            raise ScenarioRunError("PostgreSQL and Temporal did not agree on scenario completion")
        if (
            len(inbox) != len(expected_event_steps)
            or {row.id for row in inbox} != set(ingested_event_ids)
            or any(row.correlation_status != "matched" for row in inbox)
        ):
            raise ScenarioRunError("scenario events did not all use the matched durable inbox")
        if not outbox or any(row.status != "published" for row in outbox):
            raise ScenarioRunError("scenario outbox contains an unpublished delivery")
        actions_by_id = {row.id: row for row in actions}
        if set(actions_by_id) != set(expected_action_ids):
            raise ScenarioRunError("durable action requests differ from the compiled scenario")
        action_types = tuple(
            actions_by_id[action_id].action_type for action_id in expected_action_ids
        )
        expected_action_types = tuple(
            cast(str, step.reference) for _, step, _ in expected_action_steps
        )
        if action_types != expected_action_types or any(
            actions_by_id[action_id].status != "succeeded" for action_id in expected_action_ids
        ):
            raise ScenarioRunError("durable action sequence did not succeed as scripted")
        attempts_by_action = {row.action_request_id: row for row in attempts}
        if set(attempts_by_action) != set(expected_action_ids) or any(
            row.status != ActionAttemptStatus.SUCCEEDED.value for row in attempts
        ):
            raise ScenarioRunError("durable provider attempts differ from scripted actions")
        for action_id in expected_action_ids:
            action_type = actions_by_id[action_id].action_type
            expected_adapter = self._pack.simulation_bindings[action_type]
            if attempts_by_action[action_id].adapter_id != expected_adapter.id:
                raise ScenarioRunError(
                    f"action {action_type} did not use its safe simulation adapter"
                )
        approval_by_action = {
            request.action_request_id: (request, decision) for request, decision in approvals
        }
        if set(approval_by_action) != expected_approval_ids:
            raise ScenarioRunError("durable approval set differs from the compiled scenario")
        for request, decision in approval_by_action.values():
            if (
                request.status != "approved"
                or decision.decision != "approved"
                or decision.payload_hash != request.payload_hash
                or decision.actor_id != actor_id
            ):
                raise ScenarioRunError("scenario approval did not bind the exact persisted payload")
        if len(review_messages) != len(expected_approval_ids) or any(
            message.message_type != "approve" or message.actor_id != actor_id
            for message in review_messages
        ):
            raise ScenarioRunError("scenario review audit does not match scripted approvals")
        expected_turns = sum(
            step.kind in {"action", "wait", "complete"} for step in script.scenario.steps
        )
        if len(revisions) != expected_turns or len(agent.turn_inputs) != expected_turns:
            raise ScenarioRunError("durable agent-turn audit differs from the compiled scenario")
        expected_timer_wakes = sum(
            step.kind == "wait" and isinstance(script.parse_wait(step), ScenarioTimerWait)
            for step in script.scenario.steps
            if step.kind == "wait"
        )
        actual_timer_wakes = sum(
            record.reason == "timer" for record in workflow_result.wake_records
        )
        if actual_timer_wakes != expected_timer_wakes:
            raise ScenarioRunError("Temporal timer wake audit differs from the compiled scenario")
        expected_event_wakes = max(0, len(expected_event_steps) - 1)
        actual_event_wakes = sum(
            record.reason == "event" for record in workflow_result.wake_records
        )
        if actual_event_wakes != expected_event_wakes:
            raise ScenarioRunError("Temporal event wake audit differs from the compiled scenario")
        if interventions:
            raise ScenarioRunError("scenario unexpectedly required operator intervention")
        for step in script.scenario.steps:
            if step.kind == "fact":
                script.assert_fact_value(
                    step,
                    ProcessSnapshot(
                        tenant_id=tenant_id,
                        process_instance_id=process_instance_id,
                        process_type=process.process_type,
                        process_definition_version=process.definition_version,
                        status=ProcessStatus(process.status),
                        authoritative_facts=dict(process.authoritative_facts),
                        customer_claims=dict(process.customer_claims),
                    ),
                )
        project = self._pack.project
        assert project is not None
        return ScenarioResult(
            project_id=project.id,
            journey_id=script.journey.id,
            scenario_id=script.scenario.id,
            title=script.scenario.title,
            final_status=ProcessStatus(process.status),
            authoritative_facts=dict(process.authoritative_facts),
            customer_claims=dict(process.customer_claims),
            action_types=action_types,
            approval_count=len(approvals),
            trace=tuple(agent.trace),
        )

    async def _delete_tenant_data(self, tenant_id: UUID) -> None:
        models: Sequence[type[Any]] = (
            ApprovalDecision,
            ReviewMessage,
            ReviewThread,
            ApprovalRequest,
            ActionReconciliationDecision,
            ActionAttempt,
            ActionPolicyRecord,
            ActionRevision,
            ActionRequest,
            ProcessStateRevision,
            ProcessIntervention,
            ProcessControlCommand,
            OutboxRecoveryCommand,
            OutboxMessage,
            EventInbox,
            ExternalCorrelation,
            TenantCredential,
            TenantSafetyEvent,
            TenantDeploymentEvent,
            ProcessInstance,
        )
        async with self._admin_sessions.begin() as session:
            for model in models:
                await session.execute(delete(model).where(model.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
