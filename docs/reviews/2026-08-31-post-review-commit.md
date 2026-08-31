# Review of changes since `876997b (Review)`

Date: 31 August 2026

## Scope

This is a manual review of the 16 commits in `876997b..fdbd56c`, with particular
attention to the recent fictional enquiry-to-booking workflow changes and the
failure classes observed while running that journey locally.

The current uncommitted lifecycle changes were reviewed separately:

- `backend/src/tiramisu_agents/builtin/fictional_agent_output.py`
- `backend/src/tiramisu_agents/temporal/workflows/mailbox.py`
- `backend/tests/integration/test_temporal_mailbox.py`
- `backend/tests/unit/test_fictional_agent_output.py`

No source changes were made as part of the review.

## Overall assessment

The repository has a substantial foundation: tenant-scoped PostgreSQL records,
transactional Temporal delivery, typed decisions, an approval-aware action
gateway, reconciliation, process projections, a Vue operator console, and a
deterministic fictional provider pack.

It is suitable for continued local development, but it is not ready to be
treated as a dependable downstream framework yet. The main weakness is that
lifecycle truth is split between the model decision, the PostgreSQL process
projection, and the Temporal mailbox. The recent fictional fixes make the
standard path more likely to succeed, but several do so by transforming model
output or synchronising in-memory demo state rather than enforcing a single
durable lifecycle contract.

That split is the common cause behind the observed incorrect waits, invalid
decision provenance, booking/payment state mismatches, and completed database
processes whose Temporal workflows remained open.

## Findings

### P1: Invalid agent output silently parks processes

`DecisionRejected` becomes a non-retryable Temporal application error in
[`agent_turn.py`](../../backend/src/tiramisu_agents/temporal/activities/agent_turn.py#L111).
After activity retries are exhausted, the mailbox catches the error and only
adds an in-memory `TurnRecord` in
[`mailbox.py`](../../backend/src/tiramisu_agents/temporal/workflows/mailbox.py#L706).

PostgreSQL remains in its previous `active` or `waiting` state, the rejected
turn is not visible through the operator API, and no replacement wake or
operator recovery path is established. The automatic action-result chain limit
similarly clears the wake and stops in
[`mailbox.py`](../../backend/src/tiramisu_agents/temporal/workflows/mailbox.py#L674).

This turns a model/schema mistake into an apparently healthy but permanently
parked process. It directly explains the live failures involving invalid action
provenance, orphan human wakes, and waiting decisions without wake conditions.

The core needs a durable failed or intervention-required state, an operator
recovery command, and preferably a bounded correction attempt before invalid
model output is accepted as a terminal turn failure.

### P1: Schema-valid decisions can still have no progress path

[`AgentDecision`](../../backend/src/tiramisu_agents/core/contracts/decisions.py#L107)
requires a wake only for `waiting`. An `active` or `escalated` decision can
contain no action and no wake condition, producing the same `No wake condition`
dead state without raising an error.

Every non-terminal decision should deterministically produce at least one of:

- work that will be executed or reviewed;
- a valid event or timer wake;
- a durable operator/escalation state.

This should be a core policy invariant rather than a fictional prompt rule.

### P1: Action execution does not revalidate process lifecycle

Terminal and paused process checks are performed when projecting process state
in [`state.py`](../../backend/src/tiramisu_agents/processes/state.py#L81), but
action persistence occurs before that activity. The execution-time checks in
[`execution.py`](../../backend/src/tiramisu_agents/actions/execution.py#L255)
validate action status, policy outcome, approval, and payload identity without
loading the current process lifecycle state.

Review approval also does not reject a terminal or paused process. An approved
request can therefore remain executable after cancellation, manual takeover,
or another terminal transition. A partial turn can also persist a pending
action before process-state persistence rejects the decision.

Current process status, approval expiry, live policy restrictions, and other
business preconditions must be revalidated immediately before provider
dispatch.

### P1: Worker tenant assignments are not an execution boundary

[`worker.py`](../../backend/src/tiramisu_agents/temporal/worker.py#L51) uses the
configured tenant allow-list only to start tenant-specific outbox dispatcher
loops. One Temporal Worker still polls the shared task queue, and its activities
accept any active `command.tenant_id`.

A workflow placed on that queue for a different tenant can therefore be
executed by the deployment. PostgreSQL row-level security scopes each
transaction to the supplied tenant, but it does not prove that the deployment
was authorised for that tenant.

The deployment allow-list should be checked inside every tenant-bearing
activity, or deployment/task-queue isolation should make an equivalent
guarantee.

### P1: Fictional provider state is not restart-safe

Bookings, payment requests, messages, calendar entries, and provider
idempotency results live in one in-memory
[`StubBusinessState`](../../backend/src/tiramisu_agents/adapters/stubs/business.py#L82).
Payment event synchronisation is performed as a side effect of the model
activity through [`worker.py`](../../backend/src/tiramisu_agents/temporal/worker.py#L69).

If that activity completes and the worker restarts before the following
calendar action, Temporal does not rerun the successful model activity. The new
worker has empty provider state, so calendar creation cannot establish the
confirmed booking and completed payment. Similar gaps exist between booking and
payment actions.

The fictional pack is development-only, but it is also the public reference
proof for Tiramisu's recovery properties. Its provider state must be durable,
reconstructable from durable action/event records, or implemented as a
stateless fake provider outside worker memory.

### P1: Automatic payment insertion can create an invalid completed decision

[`fictional_agent_output.py`](../../backend/src/tiramisu_agents/builtin/fictional_agent_output.py#L139)
adds a `request_payment` action after a confirmed booking without changing a
model-supplied `completed` status.

That produces a decision which is simultaneously completed and contains a new
unresolved action. The action gateway persists the payment request, then
process-state projection rejects it as `completed decision has unresolved
actions` in [`state.py`](../../backend/src/tiramisu_agents/processes/state.py#L295).
The mailbox then falls into the silent failure path described above.

The current unit test covers only an `active` model output. Either the inserted
transition must derive a valid status and effective wake plan, or this
deterministic transition should live outside the model-output conversion layer.

### P1: PostgreSQL and Temporal can advertise different wake plans

Process projection retains every model wake and appends a human approval wake
in [`state.py`](../../backend/src/tiramisu_agents/processes/state.py#L143). The
mailbox instead clears model wakes while an approval or automatic action-result
chain is pending in
[`mailbox.py`](../../backend/src/tiramisu_agents/temporal/workflows/mailbox.py#L694).

The operator console can therefore display `customer.email_received` or another
business wake while Temporal is actually waiting only for approval. This is the
same class of inconsistency observed when the process said that payment must be
requested while also claiming to wait for another customer message.

One deterministic effective wake plan should be derived after action
classification and used by both PostgreSQL and Temporal.

### P1: Closing completed workflows leaves late-event delivery undefined

The current uncommitted mailbox change closes a workflow whenever the decision
status is `completed`, including when the model left stale wakes attached. That
corrects the accumulated open-workflow symptom, but
[`event ingestion`](../../backend/src/tiramisu_agents/events/ingestion.py#L112)
still schedules events correlated to terminal processes.

The dispatcher uses `REJECT_DUPLICATE` for the same stable workflow ID in
[`dispatcher.py`](../../backend/src/tiramisu_agents/temporal/dispatcher.py#L112).
Once the workflow is closed, a late matched event cannot signal the old run or
start a replacement with that ID. The outbox entry retries and eventually
fails.

Terminal closure therefore needs an explicit versioned late-event policy:
ignore, record only, quarantine, reopen, or create a new process.

### P2: Requesting revision does not create action revision lineage

A revision request supersedes the reviewed approval and action, but
[`ActionGateway`](../../backend/src/tiramisu_agents/actions/gateway.py#L112)
always creates a new action request at revision 1. The replacement has no
parent or supersedes link to the original action request.

This prevents reliable proposal diffs and means the current implementation does
not provide the action-request revision lineage described in
[`PLAN.md`](../../PLAN.md#L431). Either new `ActionRevision` rows must belong to
the same logical request, or replacement requests need an explicit lineage
relationship.

### P2: Several advertised configuration controls are not enforced

`max_follow_ups_without_reply`, minimum follow-up interval, terminal states,
and review commands are declared in
[`definitions.py`](../../backend/src/tiramisu_agents/processes/definitions.py#L26).
Only action count and maximum timer horizon enter deterministic decision policy.
Terminal states are placed in the prompt but are not used by lifecycle code.

The fictional definition also advertises `correct_fact` and `cancel`, while
[`ReviewCommandRequest`](../../backend/src/tiramisu_agents/api/processes.py#L100)
rejects both.

These fields appear to be framework policy, so they should either be enforced
outside the prompt or removed from active definitions until implemented.

### P2: Memory provenance is silently rewritten

[`fictional_agent_output.py`](../../backend/src/tiramisu_agents/builtin/fictional_agent_output.py#L178)
filters provenance IDs that are not present in the current turn. If a summary
cites one valid current source and several stale historical sources, the stale
IDs disappear but the full summary remains. The persisted summary then appears
grounded solely in the surviving source even if some of its content came from
the discarded inputs.

The whole summary should be rejected or discarded when any supplied source is
invalid. A bounded corrective model turn would be preferable to silently
changing audit provenance.

### P2: Durable outbox messages are abandoned quickly

[`TemporalOutboxDispatcher`](../../backend/src/tiramisu_agents/temporal/dispatcher.py#L57)
defaults to five delivery attempts. It permanently marks an entry failed after
the final attempt in
[`dispatcher.py`](../../backend/src/tiramisu_agents/temporal/dispatcher.py#L226).

With the current exponential delay, a Temporal outage of roughly 75 seconds can
strand an otherwise durable process. There is no requeue, dead-letter, or
operator recovery command yet.

### P2: The stock runtime is not yet downstream-extension-ready

The supplied worker entrypoint hardcodes `load_fictional_deployment()` in
[`worker.py`](../../backend/src/tiramisu_agents/temporal/worker.py#L39). The API
allows trigger rules to be injected programmatically, but the standard API and
worker commands do not load installed client packs.

A downstream editable package can import and reuse the kernel today, but it
must provide its own API and worker composition. A supported extension loader
or an explicit deployment-composition API is needed before pulling this in as a
framework gives downstream projects a stable operational boundary.

### P2: CI and smoke coverage are not currently clean

The committed tenant migration fails the CI `ruff format --check` step at
[`20260831_09_tenant_credentials_and_safety.py`](../../backend/alembic/versions/20260831_09_tenant_credentials_and_safety.py#L112).

The Playwright test in
[`operator-console.spec.ts`](../../frontend/e2e/operator-console.spec.ts#L6)
proves that the Vue console can query an empty live API, but it does not ingest
or advance a fictional process. The integration-free journey driver uses the
same adapters but bypasses Temporal, PostgreSQL process projection, model output
conversion, and worker restart behavior. Consequently, the repeated live
journey failures were not covered by either test layer.

The Continue-As-New approval/timer test also failed twice when run as part of a
suite and passed in isolation, indicating a query race around the run boundary.

## Validation performed

- Frontend unit test passed.
- Frontend TypeScript checking passed.
- Frontend production build passed.
- Ruff lint passed.
- Pyright passed with no errors or warnings.
- Alembic reported no pending model/schema operations.
- Git diff whitespace checks passed.
- Committed Temporal replay fixtures passed.
- The ordinary backend run produced 77 passes and 12 expected database skips;
  one Continue-As-New test failed and subsequently passed in isolation.
- With the local PostgreSQL integration database enabled and demo-only
  environment flags removed, 21 integration tests passed and the same
  Continue-As-New test failed intermittently.
- `ruff format --check` failed on the committed tenant migration, which means
  the current backend CI job will stop before running tests.

The local `uv` executable also encountered a systemd/snap launcher failure, so
the repository's existing `.venv` executables were used for the review checks.
This was an environment failure rather than a source-code test result.

## Local demo guide issue

The smoke-check recipe in `docs/local-demo.md` sources `.env` into the shell.
Doing so removes the inner JSON quoting from
`TIRAMISU_WORKER_TENANT_IDS=["..."]`, causing Pydantic settings parsing to fail.
It also exports the unsafe development-header and fictional-pack flags into
tests that deliberately construct production settings, causing those tests to
fail validation.

The guide should export only the two database URLs and the database-test flag,
or provide a dedicated test environment file whose values are valid both to the
shell and to Pydantic settings.

## Recommended hardening sequence

1. Define a single deterministic turn outcome containing process status,
   effective wake plan, pending actions, and terminal intent. Persist it and
   apply the same outcome in Temporal.
2. Add durable failed/intervention-required process state plus operator retry,
   wake, and takeover commands. Invalid model output and chain exhaustion must
   enter this path rather than silently sleeping.
3. Revalidate process lifecycle and live safety policy immediately before every
   provider side effect.
4. Enforce the deployment tenant boundary inside activities.
5. Move fictional provider state out of worker memory or reconstruct it from
   durable records at every provider boundary.
6. Implement terminal late-event policy before merging the unconditional
   workflow-close behavior.
7. Add real proposal lineage and make active process-definition controls
   deterministic rather than prompt-only.
8. Add a PostgreSQL + Temporal + scripted-agent integration test for the exact
   fictional journey. Restart the worker between booking, payment, and calendar
   boundaries, and assert that the PostgreSQL wake plan always matches the
   Temporal mailbox state.
9. Extend the Playwright smoke test to ingest a synthetic enquiry and display
   its real process detail and pending review.
10. Clean the formatting and local-demo environment failures so the documented
    commands and CI are reliable.

## Readiness conclusion

The Vue console and API are useful now for development and inspection. The
fictional journey can demonstrate the intended product shape when the worker
stays alive and the model follows the expected path.

Before downstream projects rely on Tiramisu as a reusable business-agent
framework, the P1 lifecycle, tenant-isolation, restart, and terminal-event
issues above should be resolved and covered by one complete Temporal-backed
reference journey.





## Dump of recent work
• The lifecycle patch did land cleanly. It closes an important race: a proposal can no
  longer be persisted or executed after the owning process becomes paused or terminal,
  and review decisions are similarly guarded. I’m now adding a durable “intervention
  required” record and explicit operator control path so model failures stop being
  invisible Temporal-only failures.

• Ran 1 command · ctrl + t to view transcript
• Ran nl -ba backend/src/tiramisu_agents/temporal/workflows/mailbox.py | sed -n
  │ '150,230p'
  │ nl -ba backend/src/tiramisu_agents/temporal/workflows/mailbox.py | sed -n '580,760p'
  │ … +4 lines
  └    150            self._buffered_reviews: list[MailboxReview] = []
       151            self._seen_review_command_ids: set[str] = set()
    … +688 lines (ctrl + t to view transcript)
    sed: can't read backend/migrations/
    versions/20260831_09_tenant_credentials_and_safety.py: No such file or directory

• Ran 2 commands · ctrl + t to view transcript

• Edited 3 files (+223 -1)
  └ backend/alembic/versions/20260831_10_process_hardening.py (+146 -0)
      1 +"""Add durable process interventions, operator controls, late-event policy, and
          lineage.
      2 +
      3 +Revision ID: 20260831_10
      4 +Revises: 20260831_09
      5 +Create Date: 2026-08-31
      6 +"""
      7 +
      8 +from collections.abc import Sequence
      9 +
     10 +import sqlalchemy as sa
     11 +from alembic import op

  └ backend/src/tiramisu_agents/db/models/__init__.py (+8 -1)

  └ backend/src/tiramisu_agents/db/models/processes.py (+69 -0)
  … Diff preview limited (ctrl + t to view transcript).

• Ran tail -140 backend/src/tiramisu_agents/db/models/processes.py && test -f backend/
  │ alembic/versions/20260831_10_process_hardening.py && echo migration-present
  └     )
        current_wake_conditions: Mapped[list[dict[str, Any]]] = mapped_column(
    … +138 lines (ctrl + t to view transcript)
        )
    migration-present

• Edited 2 files (+8 -0)
  └ backend/src/tiramisu_agents/db/models/actions.py (+1 -0)
    57      process_definition_version: Mapped[str] = mapped_column(String(64), nullable
        =False)
    58 +    supersedes_action_request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as
        _uuid=True))
    59      current_revision: Mapped[int] = mapped_column(Integer, server_default="1", n
        ullable=False)

  └ backend/src/tiramisu_agents/db/models/processes.py (+7 -0)
    34          CheckConstraint("state_version >= 0", name="state_version_nonnegative"),
    35 +        CheckConstraint(
    36 +            "late_event_policy IN ('record_only')",
    37 +            name="late_event_policy_valid",
    38 +        ),
    39          UniqueConstraint("tenant_id", "id", name="uq_process_instances_tenant_id
  … Diff preview limited (ctrl + t to view transcript).

• Edited backend/src/tiramisu_agents/db/models/actions.py (+6 -0)
    40          ),
    41 +        ForeignKeyConstraint(
    42 +            ["tenant_id", "process_instance_id", "supersedes_action_request_id"]
        ,
    43 +            ["action_requests.tenant_id", "action_requests.process_instance_id",
         "action_requests.id"],
    44 +            name="fk_action_requests_superseded_action",
    45 +            ondelete="RESTRICT",
    46 +        ),
    47          UniqueConstraint("tenant_id", "process_instance_id", "id", name="uq_acti
        on_request_ref"),

• Edited 2 files (+331 -0)
  └ backend/src/tiramisu_agents/processes/control.py (+278 -0)
      1 +"""Durable process interventions and attributed operator controls."""
      2 +
      3 +from dataclasses import dataclass
      4 +from datetime import UTC, datetime
      5 +from enum import StrEnum
      6 +from uuid import UUID, uuid4
      7 +
      8 +from sqlalchemy import select
      9 +from sqlalchemy.dialects.postgresql import insert
     10 +from sqlalchemy.ext.asyncio import AsyncSession
     11 +
     12 +from tiramisu_agents.core.contracts.decisions import HumanWakeCondition

  └ backend/src/tiramisu_agents/temporal/activities/process_state.py (+53 -0)
  … Diff preview limited (ctrl + t to view transcript).

• Explored
  └ Read mailbox.py

• Edited backend/src/tiramisu_agents/temporal/workflows/mailbox.py (+23 -0)
     24
     25 +def _activity_error_type(error: ActivityError) -> str:
     26 +    current: BaseException | None = error
     27 +    while current is not None:
     28 +        if isinstance(current, ApplicationError):
     29 +            return current.type or type(current).__name__
     30 +        current = current.__cause__
     31 +    return type(error).__name__
     32 +
     33 +
     34  @dataclass(frozen=True)
        ⋮
  … Diff preview limited (ctrl + t to view transcript).

• Edited backend/src/tiramisu_agents/temporal/workflows/mailbox.py (+29 -0)
    176          self._seen_action_resolution_ids: set[str] = set()
    177 +        self._buffered_controls: list[MailboxControl] = []
    178 +        self._seen_control_command_ids: set[str] = set()
    179          self._wake_records: list[WakeRecord] = []
        ⋮
    259
    260 +            if self._buffered_controls:
    261 +                control = self._buffered_controls.pop(0)
    262 +                self._started = True
    263 +                if control.command_type == "takeover":
    264 +                    self._wake_plan = WakePlan(human_interactions=("operator",)
         )
  … Diff preview limited (ctrl + t to view transcript).

• Edited backend/src/tiramisu_agents/temporal/workflows/mailbox.py (+35 -0)
    434              seen_action_resolution_ids=tuple(sorted(self._seen_action_resolutio
         n_ids)),
    435 +            buffered_controls=tuple(
    436 +                {
    437 +                    "command_id": control.command_id,
    438 +                    "command_type": control.command_type,
    439 +                    "event_ids": control.event_ids,
    440 +                    "review_command_ids": control.review_command_ids,
    441 +                    "action_attempt_ids": control.action_attempt_ids,
    442 +                    "timer_ids": control.timer_ids,
    443 +                }
    444 +                for control in self._buffered_controls
  … Diff preview limited (ctrl + t to view transcript).

• Edited backend/src/tiramisu_agents/temporal/workflows/mailbox.py (+65 -2)
    790                  else:
    791 -                    self._clear_wake_plan()
    791 +                    await self._record_intervention(
    792 +                        intervention_id=str(workflow.uuid4()),
    793 +                        turn_id=turn_id,
    794 +                        kind="action_chain_limit",
    795 +                        error_type="ActionChainLimitReached",
    796 +                        error="automatic action-result chain limit reached",
    797 +                        event_ids=(),
    798 +                        review_command_ids=(),
    799 +                        action_attempt_ids=result_attempt_ids,
    800 +                        timer_ids=(),
  … Diff preview limited (ctrl + t to view transcript).

• Ran 2 commands · ctrl + t to view transcript

• Edited 2 files (+20 -1)
  └ backend/src/tiramisu_agents/temporal/dispatcher.py (+19 -1)
     20      MailboxActionResolution,
     21 +    MailboxControl,
     22      MailboxEvent,
        ⋮
     87                  signal_name = "receive_event"
     87 -                signal_argument: MailboxEvent | MailboxReview | MailboxActionRe
         solution = (
     88 +                signal_argument: (
     89 +                    MailboxEvent | MailboxReview | MailboxActionResolution | Ma
         ilboxControl
     90 +                ) = (
     91                      MailboxEvent(

  └ backend/src/tiramisu_agents/temporal/worker.py (+1 -0)
  … Diff preview limited (ctrl + t to view transcript).

• Edited 2 files (+122 -1)
  └ backend/src/tiramisu_agents/api/processes.py (+121 -1)
     26  from tiramisu_agents.db.models.events import EventInbox
     27 -from tiramisu_agents.db.models.processes import ProcessInstance, ProcessStateRe
         vision
     27 +from tiramisu_agents.db.models.processes import (
     28 +    ProcessControlCommand,
     29 +    ProcessInstance,
     30 +    ProcessIntervention,
     31 +    ProcessStateRevision,
     32 +)
     33 +from tiramisu_agents.processes.control import (
     34 +    ProcessControlConflict,
     35 +    ProcessControlInput,

  └ backend/src/tiramisu_agents/security/credentials.py (+1 -0)
  … Diff preview limited (ctrl + t to view transcript).

• Ran tail -170 backend/src/tiramisu_agents/api/processes.py
  │ rg -n "record_process_intervention|state_activities" backend/src/tiramisu_agents/
  │ temporal/worker.py
  │ … +4 lines
  └ E501 Line too long (107 > 100)
      --> backend/alembic/versions/20260831_10_process_hardening.py:35:101
    … +282 lines (ctrl + t to view transcript)
    Found 16 errors.
    [*] 3 fixable with the `--fix` option.

• Edited backend/src/tiramisu_agents/events/ingestion.py (+16 -1)
     24      extension_manifest_hash: str
     25 +    late_event_policy: str = "record_only"
     26
        ⋮
     81              reason = "process_created_from_trigger"
     82 +        deliver_to_workflow = True
     83          if process_id is not None and status == "matched":
     84 +            process = await session.scalar(
     85 +                select(ProcessInstance).where(ProcessInstance.id == process_id)
     86 +            )
     87 +            if process is None:
     88 +                raise RuntimeError("matched process instance is unavailable")
  … Diff preview limited (ctrl + t to view transcript).

• Ran 4 commands · ctrl + t to view transcript

• Edited 4 files (+36 -13)
  └ backend/src/tiramisu_agents/actions/execution.py (+7 -4)
     92              await self._require_execution_enabled(session, tenant_id)
     93 -            request, action_revision, _ = await self._load_authorized_action(
     93 +            request, action_revision, _, process = await self._load_authorized_
         action(
     94                  session,
        ⋮
    144                  idempotency_key=key,
    145 +                tenant_id=tenant_id,
    146 +                process_instance_id=process_instance_id,
    147 +                authoritative_facts=dict(process.authoritative_facts),
    148              )
        ⋮

  └ backend/src/tiramisu_agents/adapters/stubs/business.py (+24 -5)

  └ backend/src/tiramisu_agents/core/ports/actions.py (+5 -1)

  └ backend/src/tiramisu_agents/temporal/worker.py (+0 -3)
  … Diff preview limited (ctrl + t to view transcript).

• Edited 2 files (+57 -6)
  └ backend/src/tiramisu_agents/actions/gateway.py (+52 -6)
     24  )
     25 -from tiramisu_agents.db.models.reviews import ReviewThread
     25  from tiramisu_agents.db.models.processes import ProcessInstance
     26 +from tiramisu_agents.db.models.reviews import ReviewMessage, ReviewThread
     27  from tiramisu_agents.db.session import set_tenant_context
        ⋮
     80              )
     81 -        return tuple(
     82 -            [
     81 +        revision_targets = await self._revision_targets(
     82 +            session,
     83 +            tenant_id=tenant_id,

  └ backend/src/tiramisu_agents/api/processes.py (+5 -0)
  … Diff preview limited (ctrl + t to view transcript).

• Ran 3 commands · ctrl + t to view transcript

• Edited 3 files (+59 -5)
  └ backend/src/tiramisu_agents/builtin/enquiry_to_booking.v1.yaml (+5 -2)
    81      - comment
    82 -    - correct_fact
    83 -    - cancel
    82 +communications:
    83 +  outbound_action_types:
    84 +    - send_message
    85 +  reply_event_types:
    86 +    - customer.email_received
    87  integrations:

  └ backend/src/tiramisu_agents/processes/definitions.py (+49 -1)
     13  from tiramisu_agents.core.contracts.processes import ProcessStatus
     14 +from tiramisu_agents.core.contracts.reviews import ReviewCommandType
     15  from tiramisu_agents.core.policy import DecisionPolicy

  └ process_definitions/examples/enquiry_to_booking.v1.yaml (+5 -2)
  … Diff preview limited (ctrl + t to view transcript).

• Explored
  └ Read state.py, process_state.py

• Edited 2 files (+10 -1)
  └ backend/src/tiramisu_agents/processes/state.py (+9 -1)
     55          decision: AgentDecision,
     56 +        terminal_states: frozenset[ProcessStatus] | None = None,
     57      ) -> AppliedProcessState:
        ⋮
    140          )
    140 -        status = self._next_status(decision=decision, open_actions=open_actions
         )
    141 +        status = self._next_status(
    142 +            decision=decision,
    143 +            open_actions=open_actions,
    144 +            terminal_states=terminal_states,
    145 +        )

  └ backend/src/tiramisu_agents/temporal/activities/process_state.py (+1 -0)
  … Diff preview limited (ctrl + t to view transcript).

• Edited 2 files (+31 -8)
  └ backend/src/tiramisu_agents/api/main.py (+14 -8)
    11  from tiramisu_agents import __version__
    12 -from tiramisu_agents.api.events import fictional_trigger_rules
    12  from tiramisu_agents.api.events import router as events_router
       ⋮
    16  from tiramisu_agents.events.ingestion import ProcessBootstrap
    17 +from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
    18
       ⋮
    41      trigger_rules: Mapping[str, ProcessBootstrap] | None = None,
    42 +    process_registry: ProcessDefinitionRegistry | None = None,
    43  ) -> FastAPI:
       ⋮

  └ backend/src/tiramisu_agents/api/processes.py (+17 -0)
  … Diff preview limited (ctrl + t to view transcript).

• Explored
  └ Read test_operator_api.py, test_api.py, settings.py

• Edited 2 files (+90 -1)
  └ backend/src/tiramisu_agents/actions/gateway.py (+70 -0)
      4  from dataclasses import dataclass
      5 +from datetime import datetime, timedelta
      6  from hashlib import sha256
        ⋮
     25  )
     26 +from tiramisu_agents.db.models.events import EventInbox
     27  from tiramisu_agents.db.models.processes import ProcessInstance
        ⋮
     46
     47 +@dataclass(frozen=True, slots=True)
     48 +class CommunicationPolicy:
     49 +    outbound_action_types: frozenset[str]

  └ backend/src/tiramisu_agents/temporal/activities/action_gateway.py (+20 -1)
  … Diff preview limited (ctrl + t to view transcript).

• Ran head -25 backend/src/tiramisu_agents/temporal/activities/action_gateway.py
  │ head -30 backend/src/tiramisu_agents/actions/gateway.py
  └ """Temporal Activity boundary for idempotent action classification and
    persistence."""
    … +52 lines (ctrl + t to view transcript)
    from tiramisu_agents.db.session import set_tenant_context


• Edited backend/src/tiramisu_agents/temporal/activities/action_gateway.py (+1 -1)
    4  from dataclasses import asdict, dataclass
    5 -from datetime import datetime
    5 +from datetime import datetime, timedelta
    6  from uuid import UUID

────────────────────────────────────────────────────────────────────────────────────────

• The durable recovery path is now taking shape: failed turns and chain-limit stops are
  persisted as operator-visible interventions, and retry/wake/takeover/resume are
  attributed database commands delivered through the outbox. I’ve also made approval/
  operator wakes part of the Temporal query state, so PostgreSQL and Temporal can be
  compared directly instead of translating “no wake plan” by convention.

• Ran .venv/bin/ruff format backend/src/tiramisu_agents/actions/execution.py backend/
  │ src/tiramisu_agents/actions/gateway.py backend/src/tiramisu_agents/adapters/stubs/
  │ business.py backend/src/tiramisu_agents/api/main.py backend/src/tiramisu_agents/
  │ … +26 lines
  └ /home/chrisutherland/ember/ember/tiramisu-agents/backend/src/tiramisu_agents/
    adapters/stubs/business.py
    … +8 lines (ctrl + t to view transcript)
    Unknown]" (reportUnknownVariableType)
    2 errors, 0 warnings, 0 informations

• Edited 2 files (+7 -3)
  └ backend/src/tiramisu_agents/adapters/stubs/business.py (+6 -2)
      5  from datetime import UTC, datetime, timedelta
      6 -from typing import Any
      6 +from typing import Any, cast
      7  from uuid import UUID
        ⋮
    387          available_slots = (
    388 -            tuple(value for value in durable_slots if isinstance(value, str))
    388 +            tuple(
    389 +                value
    390 +                for value in cast(list[object], durable_slots)
    391 +                if isinstance(value, str)
    392 +            )

  └ backend/src/tiramisu_agents/core/ports/actions.py (+1 -1)
  … Diff preview limited (ctrl + t to view transcript).

• Ran 2 commands · ctrl + t to view transcript
• Ran ls backend | sort
  │ TIRAMISU_RUN_DB_TESTS=1 .venv/bin/pytest backend/tests/integration/
  │ test_process_state.py backend/tests/integration/test_action_gateway.py backend/
  │ … +3 lines
  └ FFFFFFFFFF                                                               [100%]
    =================================== FAILURES ===================================
    … +3036 lines (ctrl + t to view transcript)
    and_suspension
    10 failed in 4.80s

────────────────────────────────────────────────────────────────────────────────────────

• Unit and strict type checks are green. The first PostgreSQL run hit local test-
  environment setup—not code—the expected tiramisu database role does not exist on this
  Postgres instance. I’ll keep implementation moving, then provision an isolated
  tiramisu_test role/database only if the local account has permission; I won’t touch
  the user’s existing application database.

• Ran 2 commands · ctrl + t to view transcript
• Ran .venv/bin/pytest backend/tests/integration/test_temporal_mailbox.py backend/tests/
  │ unit/test_business_stub_adapters.py backend/tests/unit/test_process_definitions.py
  │ backend/tests/unit/test_action_policy.py -q
  └ .......FFF...........                                                    [100%]
    =================================== FAILURES ===================================
    … +401 lines (ctrl + t to view transcript)
    ion_and_timer
    3 failed, 18 passed in 4.63s

• Edited 2 files (+14 -4)
  └ backend/src/tiramisu_agents/temporal/workflows/mailbox.py (+8 -1)
    805              elif requires_approval or self._pending_action_request_ids:
    806 -                self._apply_turn_outcome(process_state_result, decision_json=de
         cision_json)
    806 +                if "terminal" in process_state_result:
    807 +                    self._apply_turn_outcome(process_state_result, decision_jso
         n=decision_json)
    808 +                else:
    809 +                    # Compatibility for pre-outcome histories and workflow-only
    810 +                    # tests: a persisted approval always owns the effective wak
         e.
    811 +                    self._wake_plan = WakePlan(human_interactions=("approval",)
         )

  └ backend/tests/integration/test_temporal_mailbox.py (+6 -3)
  … Diff preview limited (ctrl + t to view transcript).

• Ran 1 command · ctrl + t to view transcript
• Ran TIRAMISU_RUN_DB_TESTS=1 TIRAMISU_MIGRATION_DATABASE_URL='postgresql+asyncpg://
  │ tiramisu:tiramisu@localhost:5432/tiramisu_test'
  │ TIRAMISU_DATABASE_URL='postgresql+asyncpg://
  │ … +5 lines
  └ FFFFFFFFFF                                                               [100%]
    =================================== FAILURES ===================================
    … +3832 lines (ctrl + t to view transcript)
    and_suspension
    10 failed in 5.06s

• Ran 1 command · ctrl + t to view transcript
• Ran TIRAMISU_RUN_DB_TESTS=1 TIRAMISU_MIGRATION_DATABASE_URL='postgresql+asyncpg://
  │ tiramisu:tiramisu@localhost:5432/tiramisu_test'
  │ TIRAMISU_DATABASE_URL='postgresql+asyncpg://
  │ … +5 lines
  └ .F.....F..                                                               [100%]
    =================================== FAILURES ===================================
    … +522 lines (ctrl + t to view transcript)
    test_operator_api.py::test_operator_can_inspect_process_and_approve_exact_proposal
    2 failed, 8 passed in 5.10s

• Edited 2 files (+10 -0)
  └ backend/src/tiramisu_agents/actions/gateway.py (+9 -0)
    101          for action in decision.actions:
    102 +            existing_action_id = await session.scalar(
    103 +                select(ActionRequest.id).where(
    104 +                    ActionRequest.tenant_id == tenant_id,
    105 +                    ActionRequest.process_instance_id == process_instance_id,
    106 +                    ActionRequest.agent_turn_id == agent_turn_id,
    107 +                    ActionRequest.logical_action_key == action.logical_action_k
         ey,
    108 +                )
    109 +            )
    110              if (
    111 +                existing_action_id is None

  └ backend/tests/integration/test_operator_api.py (+1 -0)
  … Diff preview limited (ctrl + t to view transcript).

• Explored
  └ Read gateway.py

• Edited backend/src/tiramisu_agents/actions/gateway.py (+1 -0)
    111                  existing_action_id is None
    112 +                and
    113                  communication_policy is not None

• Ran 4 commands · ctrl + t to view transcript

• Edited backend/tests/integration/test_temporal_mailbox.py (+3 -3)
      66          assert state.buffered_events == (payment,)
      67 -        assert state.wake_plan is not None
      68 -        assert state.wake_plan.human_interactions == ("approval",)
      67 +        assert state.wake_plan is None
      68
         ⋮
    1044          assert state.buffered_events == (buffered_event,)
    1046 -        assert state.wake_plan is None
    1045 +        assert state.wake_plan is not None
    1046 +        assert state.wake_plan.human_interactions == ("approval",)
    1047

• Ran 2 commands · ctrl + t to view transcript

• Edited backend/src/tiramisu_agents/testkit/scripted_agent.py (+7 -3)
     3  from collections import deque
     4 -from collections.abc import Iterable
     4 +from collections.abc import Callable, Iterable
     5
       ⋮
    10  class ScriptedAgent:
    11 -    def __init__(self, decisions: Iterable[AgentDecision]) -> None:
    11 +    def __init__(
    12 +        self,
    13 +        decisions: Iterable[AgentDecision | Callable[[AgentTurnInput], AgentDeci
        sion]],
    14 +    ) -> None:
  … Diff preview limited (ctrl + t to view transcript).

────────────────────────────────────────────────────────────────────────────────────────

• The fictional adapters are now restart-safe at their boundary: each provider request
  carries the durable process facts, so booking, payment, and calendar validation no
  longer depends on the worker’s old in-memory stub object. I’m wiring the real
  PostgreSQL + Temporal journey now, deliberately replacing the worker between each
  major handoff.

• Added backend/tests/integration/test_full_scripted_journey.py (+568 -0)
      1 +"""Full PostgreSQL + Temporal journey with worker restarts at provider boundari
         es."""
      2 +
      3 +import asyncio
      4 +import os
      5 +from contextlib import asynccontextmanager
      6 +from datetime import UTC, datetime
      7 +from typing import AsyncIterator
      8 +from uuid import UUID, uuid4
      9 +
     10 +import pytest
     11 +from sqlalchemy import delete, select
  … Diff preview limited (ctrl + t to view transcript).

• Ran .venv/bin/ruff format backend/src/tiramisu_agents/testkit/scripted_agent.py
  │ backend/tests/integration/test_full_scripted_journey.py
  │ .venv/bin/ruff check backend/src/tiramisu_agents/testkit/scripted_agent.py backend/
  │ … +3 lines
  └ /home/chrisutherland/ember/ember/tiramisu-agents/backend/tests/integration/
    test_full_scripted_journey.py
    … +272 lines (ctrl + t to view transcript)
    (reportDeprecated)
    64 errors, 0 warnings, 0 informations

• Edited backend/tests/integration/test_full_scripted_journey.py (+44 -26)
      4  import os
      5 +from collections.abc import AsyncGenerator
      6  from contextlib import asynccontextmanager
      7  from datetime import UTC, datetime
      7 -from typing import AsyncIterator
      8 +from typing import Any, cast
      9  from uuid import UUID, uuid4
        ⋮
     18  from tiramisu_agents.adapters.registry import ActionAdapterRegistry
     18 -from tiramisu_agents.adapters.stubs import StubBusinessState, stub_business_bin
         dings
     19 +from tiramisu_agents.adapters.stubs import StubBusinessState
  … Diff preview limited (ctrl + t to view transcript).

• Ran 1 command · ctrl + t to view transcript
• Ran TIRAMISU_RUN_DB_TESTS=1 TIRAMISU_MIGRATION_DATABASE_URL='postgresql+asyncpg://
  │ tiramisu:tiramisu@localhost:5432/tiramisu_test'
  │ TIRAMISU_DATABASE_URL='postgresql+asyncpg://
  │ … +2 lines
  └ 2026-08-31T05:18:56.053765Z  WARN temporalio_sdk_core::worker: Temporal Server
    1.16.0 or newer is required to guarantee that the latest heartbeat details are
    … +334 lines (ctrl + t to view transcript)
    test_full_scripted_journey.py::test_full_scripted_journey_survives_worker_restarts
    1 failed in 10.72s

• Explored
  └ Read test_full_scripted_journey.py

• Edited backend/tests/integration/test_full_scripted_journey.py (+2 -1)
    144      if action_type == "propose_booking":
    145 +        result_facts = {fact.key: fact.value for fact in turn.action_results[0]
         .facts}
    146          return _new_decision(
        ⋮
    153                      parameters={
    153 -                        "booking_reference": turn.process.authoritative_facts["
         booking.reference"],
    154 +                        "booking_reference": result_facts["booking.reference"],
    155                          "amount_minor": 12_500,