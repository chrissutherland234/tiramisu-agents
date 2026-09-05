# Tiramisu — Long-Running Business Agents Project Plan

Status: Draft  
Last updated: 2026-09-05

Working project name: **Tiramisu**  
Name rationale: “Pick me up” reflects agents waking from durable waits and continuing with the same context. The layered dessert also reflects the platform's workflow, memory, policy, and integration layers.
Intended repository model: public source under the **MIT License**, with client-specific implementation packs kept in separate private repositories.

## 1. Product vision

Build a managed, multi-tenant platform for long-running AI agents that follow a single customer journey or business case through its complete lifecycle.

Examples include an enquiry-to-booking journey, sale, claim, application, case, or service engagement. A journey may reference several business objects over time: an enquiry, customer, quote, booking, payment, calendar event, and external message thread. Each process instance has one logical agent and one durable context. The agent reacts to business events, performs bounded work, and then sleeps until one of its approved wake conditions occurs.

A representative journey is:

> Website enquiry → customer conversation → booking → confirmation → calendar update → payment → service completion → follow-up

The agent must survive process restarts, deployments, infrastructure failures, and waits lasting days or months without losing its identity, context, decisions, or outstanding commitments.

## 2. Core product invariants

1. One process instance represents one stable customer journey, case, or business intent; it may correlate to many business objects and provider resources.
2. One logical agent is attached to each process instance.
3. The agent identity and durable context remain stable for the life of the process.
4. An agent runs only for bounded reasoning turns; it does not remain resident in memory while waiting.
5. Temporal owns workflow execution, timers, signals, retries, and wake-up behavior.
6. PostgreSQL owns application records, tenant configuration, agent memory, received-event and audit ledgers, and UI projections.
7. OpenAI is used for reasoning and tool selection, but is not the system of record.
8. The model may propose only typed, policy-constrained actions and wake conditions.
9. Deterministic workflow and policy code owns lifecycle invariants, terminal conditions, authorization, and whether a proposal may execute.
10. External side effects must be idempotent where supported, auditable, and reconcilable when the result is ambiguous.
11. Every running process is pinned to compatible immutable configuration versions unless explicitly migrated; emergency safety restrictions may tighten live.
12. The agent kernel depends on provider-neutral business capabilities, never vendor SDKs.
13. Every mutating action passes through the same authorization, policy, approval, and execution gateway.
14. Each process permits only one reasoning turn or mutating command to be committed at a time.
15. All autonomy is bounded by process, communication, cost, rate, and lifetime limits enforced outside the prompt.
16. Sensitive data is minimized in Temporal history, model context, logs, traces, and search attributes.
17. The public platform defines stable, contract-tested extension points; client-specific code and configuration never become hidden dependencies of the public repository.

## 3. Recommended technology stack

### Backend

- Python 3.13
- FastAPI
- Temporal Python SDK
- OpenAI Agents SDK using the Responses API
- Pydantic for API and agent contracts
- SQLAlchemy 2 with async PostgreSQL support
- Alembic for database migrations
- `uv` for Python environments and dependency management

### Frontend

- Vue 3
- TypeScript
- Vite
- Vue Router
- Pinia or a query-focused state library, selected during scaffolding

### Infrastructure and operations

- PostgreSQL
- Temporal development environment for local work
- Docker Compose for local dependencies
- OpenTelemetry-compatible tracing and metrics
- Structured JSON logging
- CI for formatting, linting, typing, tests, migrations, and Temporal replay checks

## 4. High-level architecture

```text
Business event, webhook, operator action, or timer
                         │
                         ▼
                  Event ingestion API
               validate → dedupe → persist
                         │
                         ▼
             Event router and correlation registry
            resolve → quarantine ambiguity → dispatch
                         │
                         ▼
       Temporal AgentWorkflow (one per journey/case)
                         │
                         ├── Load durable context from PostgreSQL
                         ├── Run one bounded, proposal-only OpenAI agent turn
                         ├── Validate proposed actions and wake conditions
                         ├── Route actions through permission gateway
                         ├── Wait durably for required human input
                         ├── Execute approved actions as Activities
                         ├── Reconcile ambiguous provider outcomes
                         ├── Resolve tenant provider through integration ports
                         ├── Persist results, memory, and audit records
                         └── Wait for Signals and/or durable Timers
```

### Service boundaries

- **API service:** authentication, tenant administration, process APIs, operator APIs, webhooks, and read models for the frontend.
- **Temporal worker:** workflow definitions and activities for reasoning, policies, context maintenance, approvals, and side effects.
- **Vue application:** operator visibility, approvals, manual intervention, process configuration, and tenant administration.
- **PostgreSQL:** application persistence. Temporal persistence should use a separate database and database role if self-hosted on the same PostgreSQL server.
- **Event router and correlation registry:** maps provider IDs, message threads, customer identifiers, and related business resources to a stable process instance; quarantines unmatched or ambiguous events for operator resolution.
- **Action gateway:** the single path from a proposed business action through capability checks, policy evaluation, approval, idempotency, execution, and audit.
- **Integration ports and adapters:** provider-neutral interfaces backed by email, payment, calendar, booking, CRM, stub, and client-specific implementations.

### Open-source and private extension boundary

The generic Tiramisu platform is developed in a public MIT-licensed monorepo. The public repository includes the deterministic kernel, Temporal orchestration, API, generic Vue operator application, database schema and migrations, policy and approval framework, process-definition contracts, test kit, stub adapters, generally reusable provider adapters, fictional examples, and deployment documentation.

Client implementations are separate private packages and repositories. A client pack may contain:

- Client-specific process definitions, prompts, terminology, policies, and evaluations
- Proprietary mappings and provider adapters
- Deployment composition and infrastructure configuration
- Tests and synthetic fixtures for the client's contracted behavior
- References to secrets held in a secret manager, but never secret values or customer data

The public repository must build, test, document, and run its fictional reference journey without any private package. A private client pack pins a compatible Tiramisu version and must pass the public process, policy, adapter, migration, and replay contract suites before deployment.

Worker startup composes a deployment through an explicit, versioned extension manifest. It registers allowed process definitions, adapters, policies, Activities, and capabilities before workers begin polling. Temporal workflow code refers only to stable registered identifiers and versioned contracts; it never performs filesystem scanning, network-based plugin discovery, or nondeterministic imports during workflow execution.

Client extensions may narrow capabilities and policy. The supported registration path cannot replace the workflow or action gateway, but an installed Python pack is trusted executable deployment code, not a sandbox. It requires source review, immutable builds, dependency controls, and deployment isolation; protection from malicious pack code requires a process or service boundary. Client-specific database tables and migrations are deferred in the first release; client packs should use the public extension model and authoritative external systems unless a reviewed schema-extension mechanism is introduced later.

A gitignored local directory may hold disposable `.env` files, generated artifacts, and developer-only overrides. It is never the canonical home of client source, prompts, policies, process definitions, evaluations, or deployment configuration. Canonical private work requires a private repository, history, review, and CI.

### Authority and consistency boundaries

Temporal, PostgreSQL, and external providers do not share an atomic transaction. Authority is therefore explicit:

| Concern | Authority |
| --- | --- |
| Current orchestration state, wake conditions, and command eligibility | Temporal workflow |
| Booking, payment, calendar, and CRM facts | The owning provider or domain service |
| Received-event ledger, configuration, audit, and durable application memory | PostgreSQL |
| Operator and client read models | Rebuildable PostgreSQL projections |
| Model-generated text, proposals, summaries, and inferences | Non-authoritative until validated or applied |

An approval decision or external event may be durably recorded in PostgreSQL but later rejected as stale, duplicate, ambiguous, or no longer applicable by the workflow. Every Signal or Update references a persisted event or command ID. Transactional inbox/outbox delivery, idempotent consumers, reconciliation jobs, and projection watermarks make the boundary recoverable rather than pretending it is exactly-once.

## 5. Durable agent execution model

An agent is a stable logical identity, not a permanently running Python object. On every wake cycle, the workflow reconstructs the context required for the next bounded turn.

### Initial execution topology

For the first vertical slice, the OpenAI Agents SDK runs as a proposal-only Temporal Activity. It receives a prepared, bounded context package and returns a typed `AgentDecision`. The agent may use read-only reasoning helpers inside that Activity, but it does not directly execute mutating provider tools. The workflow, action gateway, and side-effect Activities remain responsible for state transitions, approval, execution, and durable waiting.

This is deliberately separate from Temporal's OpenAI Agents SDK integration, which can orchestrate the runner in workflow code and route model/tool calls through Activities. That integration will be evaluated in an isolated spike. Adoption requires successful replay, retry, session-idempotency, human-approval, Continue-As-New, and worker-upgrade tests. The agent-kernel and decision contracts must remain usable without the integration so it can be adopted or replaced without rewriting the business process.

### Wake cycle

1. Receive one or more canonical business events through the per-process mailbox.
2. Deduplicate and persist the events.
3. Load the process instance, pinned definition, agent memory, and relevant business facts.
4. Assemble a bounded context package.
5. Run one proposal-only OpenAI Agents SDK invocation inside a Temporal Activity.
6. Require a structured `AgentDecision` result.
7. Validate the decision against current process state, deterministic lifecycle rules, tenant policy, and autonomy budgets.
8. If deterministic validation rejects the proposal, allow at most two complete replacement proposals using the exact controlled error, rejected proposal, identical loaded context, and unchanged workflow timestamp. Correction feedback is not business evidence; exhaustion fails closed into operator intervention.
9. Create durable approval requests for actions that require human authorization.
10. Execute allowed or approved actions through idempotent Activities.
11. Resolve each business capability to the tenant's configured integration adapter.
12. Persist action attempts and outcomes; reconcile any ambiguous result before retrying an unsafe side effect.
13. Update durable memory without allowing summaries or inferences to overwrite authoritative facts.
14. Install approved event and timer wake conditions.
15. Wait for the next event, timeout, cancellation, or operator intervention.

Model calls, database access, and network calls must not run directly in Temporal workflow code. They belong in Activities so workflow execution remains deterministic and replay-safe. The workflow contains only deterministic orchestration and state. Hard lifecycle transitions, completion criteria, approval requirements, and safety restrictions are application rules; the model chooses only among allowed proposals.

### Mailbox and concurrency rules

- Only one agent reasoning turn may commit for a process at a time.
- Signals, Updates, and timers that arrive during a turn are appended to the mailbox and processed deterministically afterward.
- Compatible events may be coalesced into the next context package without losing their individual event IDs.
- Higher-priority cancellation, takeover, and safety commands pre-empt ordinary business events at the next deterministic boundary.
- An approved action is revalidated immediately before execution if relevant facts or process state changed while approval was pending.
- Timer-versus-event ties, late events, handler completion, and Continue-As-New boundaries have explicit tests and stable ordering rules.

### Illustrative agent decision

```json
{
  "decision_id": "dec_01J...",
  "based_on_event_ids": ["evt_01J..."],
  "status": "waiting",
  "actions": [
    {
      "logical_action_key": "booking_follow_up_1",
      "type": "send_email",
      "parameters": {
        "template": "booking_follow_up"
      }
    }
  ],
  "wake_conditions": [
    {
      "type": "event",
      "event_type": "customer.email_received"
    },
    {
      "type": "event",
      "event_type": "payment.completed"
    },
    {
      "type": "timer",
      "at": "2026-09-01T09:00:00Z"
    }
  ],
  "memory_update": {
    "summary": "Waiting for the customer to confirm or complete payment.",
    "summary_source_event_ids": ["evt_01J..."],
    "open_commitments": [
      "Follow up if no reply is received before the timer expires."
    ]
  }
}
```

The model does not create arbitrary executable predicates. It selects from registered event types, action types, and bounded timer specifications. Application policy can accept, reject, modify, or route a proposal for human approval.

## 6. Agent context and memory

The same logical context follows the journey or case, but the full transcript must not grow without limit. Context should be layered:

- Current authoritative business facts and deterministic process state
- Customer-stated claims, including whether they are verified or disputed
- Agent inferences and summaries, explicitly marked as non-authoritative
- Goals, constraints, and process-definition instructions
- Outstanding commitments and wake conditions
- Recent messages and events
- Relevant previous action results
- Immutable event and action ledger
- Older detail retrieved only when needed

Each memory item or compacted fact should record its source/provenance, observation time, validity time where relevant, sensitivity classification, and status or confidence. A model-written summary must never replace an authoritative provider fact. Corrections and superseded facts remain auditable.

The initial implementation uses application-controlled PostgreSQL history and generated context snapshots as the single conversation-continuation strategy. SDK sessions, OpenAI Conversations, and `previous_response_id` are not mixed with local replay. An ADR may later select a custom SDK session or a provider-managed continuation strategy, but retries must make session writes idempotent by process and agent-turn ID, and PostgreSQL must remain the source of recoverable business context.

Memory has two scopes:

- **Process memory:** facts, messages, commitments, approvals, and outcomes needed for this journey.
- **Customer memory:** optional cross-process preferences, identity, communication consent, and relationship history, subject to explicit tenant policy and purpose limitation.

Information must not cross between processes merely because it may help the model. Cross-process retrieval requires a permitted customer-memory purpose and an auditable source.

Memory compaction must preserve:

- Confirmed facts and identifiers
- Concise decision rationale and the evidence used, not hidden model chain-of-thought
- Completed external actions and outcomes
- Active assumptions
- Customer preferences and promises
- Unresolved blockers
- Outstanding approvals
- The next objective and wake conditions

## 7. Events, wake conditions, and actions

### Canonical events

Integration-specific webhooks are normalized into versioned canonical events, such as:

- `enquiry.created`
- `customer.email_received`
- `booking.created`
- `booking.confirmed`
- `booking.cancelled`
- `payment.requested`
- `payment.completed`
- `payment.failed`
- `calendar.updated`
- `approval.granted`
- `approval.rejected`
- `approval.revision_requested`
- `approval.review_message_received`
- `operator.message_received`

Each event carries an event ID, tenant ID, source, source event ID, occurrence time, receipt time, schema version, sensitivity classification, and typed payload. It includes the stable process-instance ID after correlation, plus zero or more external resource references. Ingestion must not assume that a provider webhook already knows the process instance.

### Event routing and external correlation

The event router resolves an incoming event using a registry of external references:

```text
tenant + provider + resource type + external ID → process instance
```

References include enquiry IDs, booking IDs, payment IDs, calendar event IDs, CRM records, email message/thread IDs, and safe customer identifiers. Correlation rules are deterministic and auditable. Exact provider references take precedence over secondary identity or time-based matching.

- Unmatched or ambiguous events enter a quarantine queue and never wake a guessed process.
- Operators can resolve quarantine items, creating an audited correlation and dispatching the original event.
- Correlations are idempotent and tenant-scoped; conflicting mappings require intervention.
- Late events are retained but may be ignored, recorded only, reopen a process, or start a new process according to versioned policy.
- Merge, split, alias, and reopen behavior is explicit for process types that need it.
- Signal-With-Start is used only after the router has derived a stable, tenant-scoped process key.

### Wake conditions

Initially supported wake conditions:

- A canonical event type, optionally with a safe typed filter
- An absolute time
- A duration from the current workflow time
- Human approval or rejection
- Human review message or revision request
- Operator intervention
- Cancellation or process termination

Temporal Signals or Updates will deliver external events. Durable Timers will implement scheduled wake-ups. Signal-With-Start should be used where a business event may create the process if it does not already exist.

Every proposed wake condition is validated against allowed event types, maximum timer horizon, minimum follow-up interval, communication policy, process lifetime, and per-process wake budget. The model cannot invent executable predicates or schedule unbounded polling.

### Actions

Actions are registered capabilities with typed inputs, policy metadata, idempotency behavior, and an integration implementation. Example actions include:

- Send email
- Request information
- Create or modify a booking
- Create a payment link
- Update a calendar
- Add a CRM note
- Escalate to an operator
- Mark a process complete

The model proposes actions; the application authorizes and executes them.

### Action lifecycle and ambiguous outcomes

An action has an application-level state machine independent of a single Activity attempt:

```text
PROPOSED
  ├── DENIED ──────────────────────────────── terminal
  ├── AWAITING_APPROVAL
  │     ├── REJECTED / EXPIRED ────────────── terminal
  │     ├── SUPERSEDED ────────────────────── terminal for this revision
  │     └── APPROVED ───────────────────┐
  └── ALLOWED ──────────────────────────┤
                                        ▼
                                    EXECUTING
                                        │
                         ┌──────────────┼──────────────┐
                         ▼              ▼              ▼
                    SUCCEEDED         FAILED         UNKNOWN
                                                        │
                                                        ▼
                                                  RECONCILING
                                                 ┌──────┴──────┐
                                                 ▼             ▼
                                            SUCCEEDED       FAILED
```

`UNKNOWN` means the provider may have accepted the operation even though the Activity did not receive a conclusive response. The adapter must use provider idempotency when available, otherwise lookup-before-retry and provider-specific reconciliation. Financial, customer-visible, or otherwise unsafe actions use bounded Activity attempts and never rely on Temporal's default unlimited retry behavior. Compensation is modeled explicitly where a provider supports it; irreconcilable outcomes require operator intervention.

### Provider-neutral integration layer

The kernel and agent prompts operate on stable business primitives rather than provider APIs. For example, the agent sees `find_available_slots`, `confirm_booking`, `send_customer_message`, and `request_payment`; it does not see Google Calendar, Microsoft Graph, Stripe, or a particular email vendor.

Each capability has a typed port with real and stub implementations:

```text
CalendarPort
├── StubCalendar
├── GoogleCalendar
├── MicrosoftCalendar
└── ClientBookingSystemCalendar

MessagingPort
├── StubMessaging
├── TransactionalEmailProvider
└── ClientCRMMessageProvider

PaymentPort
├── StubPayment
└── StripePayment
```

Process configuration binds the provider-neutral capability to a tenant connection:

```yaml
integrations:
  calendar:
    provider: google_calendar
    connection: primary_calendar
  messaging:
    provider: transactional_email
    connection: customer_email
  payments:
    provider: stripe
    connection: nz_account
```

The adapter registry resolves these bindings only inside Activities. The agent kernel must not import provider SDKs, handle OAuth credentials, or depend on provider payload formats. Provider webhooks are translated into the same canonical events produced by stub adapters.

### Action permission gateway

Every mutating action follows one pipeline:

```text
Agent proposes typed action
          ↓
Schema and invariant validation
          ↓
Tenant and process capability check
          ↓
Business constraint evaluation
          ↓
Policy decision
  ┌───────────┼──────────────────────┐
ALLOW        DENY          REQUIRE_APPROVAL
  │            │                     │
Execute     Record reason      Create approval request
                                      │
                               Temporal waits durably
                                      │
                      approve / reject / revise / comment / expire
```

The policy result is one of:

- `ALLOW`: execute the exact requested action.
- `DENY`: do not execute; record a reason and return the result to the agent.
- `REQUIRE_APPROVAL`: create an approval request and wait durably.

Policies are data and deterministic application code, not prompt-only instructions. They may consider tenant configuration, action type, amount, journey and related-resource state, customer consent, process stage, operator role, previous approvals, and risk level.

An approval is bound to:

- Tenant, process instance, and action-request ID
- Review thread, proposal revision, and any superseded action-request ID
- Exact action type and serialized parameter hash
- Agent turn and process-definition version
- Policy version and reason for approval
- Required role, group, or approval quorum
- Creation and expiry time
- Approver identity and optional decision reason

Changing a material action parameter invalidates the approval. Retrying the same approved action retains its idempotency key but does not broaden the approval scope. Feedback that changes the proposal creates a new action-request revision with a new parameter hash; the old request becomes `SUPERSEDED` and cannot execute.

Approval does not guarantee execution. The workflow revalidates current state, policy, payload hash, expiry, budgets, and external preconditions immediately before dispatch. An approval can therefore be recorded and then rejected as stale without losing its audit history.

### Human-in-the-loop behavior

Human involvement is a first-class domain state and includes:

- Approval or rejection of a proposed action
- Conversational review of a proposal, including suggestions and requests to try again
- A request for missing information
- Expert consultation
- Manual correction of business facts
- Escalation
- Manual takeover and later return to autonomous operation

### Conversational review and revision

An operator can have a durable, process-scoped conversation with the same logical agent while an action is awaiting review or whenever the process permits operator guidance. Each message is stored as an attributed interaction event and wakes one bounded agent turn; there is no permanently resident chat process.

Approval review supports these explicit commands:

- **Approve:** authorize the exact current proposal and parameter hash.
- **Reject:** terminate the current proposal with a reason.
- **Request revision:** provide feedback such as “make this warmer,” “offer Tuesday instead,” or “try something more like this.” The current proposal becomes `SUPERSEDED`; the same agent receives the feedback and relevant review thread, then creates a new proposal revision.
- **Ask or comment:** continue the review conversation without authorizing execution. The agent may answer, request information, or produce a new proposal.
- **Correct fact:** submit a typed correction through the appropriate domain command. Free-text feedback alone never silently changes an authoritative business fact.
- **Expire or cancel:** close the review without execution.

### Operator reevaluation versus fact correction

`Wake` and `Resume` are reevaluation controls, not fact mutations. They persist an attributed
`operator.manual_wake` kernel event and ask the same logical agent to run one bounded turn against
the currently recorded state. The event bypasses the agent-selected business-event filter, clears
the previous wake plan, is consumed once by its durable event ID, and lets the resulting decision
install a new plan. Review commands, action resolutions, and process controls retain priority;
manual reevaluation then precedes an ordinary matching event or the superseded timer.

The operator reason and actor are visible to the turn as guidance, but the manual-wake event carries
no fact observations. Free text such as “assume the customer paid cash” therefore cannot change
`payment.status`, satisfy a deterministic action precondition, or masquerade as a provider result.
External ingress and tenant process definitions cannot create or subscribe to reserved kernel event
types.

Changing a business fact is a separate permissioned domain operation. A future admin fact editor
may derive typed fields from the process definition, but it must validate the complete domain
command, capture actor and evidence, be idempotent and append-only/audited, and produce an explicit
correction or provider-equivalent canonical event. It must never edit projection JSON directly.
Existing action reconciliation remains limited to evidence-backed resolution of an ambiguous action
attempt; recording an offline cash payment when there is no ambiguous attempt requires a payment
domain command.

```text
PENDING_REVIEW
  ├── approve exact revision ──────────────── APPROVED → revalidate → execute
  ├── reject / expire / cancel ────────────── closed without execution
  ├── ask or comment ── agent turn ────────── response and/or new revision
  └── request revision ── supersede proposal
                              │
                              ▼
                     bounded agent turn
                              │
                              ▼
                    NEW PENDING_REVIEW
```

The UI must not offer an ambiguous “approve with changes” operation. If feedback can change recipients, content, timing, amount, booking details, or another material parameter, it is a revision request followed by review of the new exact proposal. A non-material note may accompany approval for audit or later work, but it cannot mutate the approved action.

Review threads preserve message authorship, timestamps, visibility, referenced proposal revision, source event IDs, attachments by opaque reference, and proposal lineage. Messages may be batched before the next agent turn, but they are never lost during retries or Continue-As-New. Permission policy controls who may view, comment, revise, approve, or correct facts.

The recommended Temporal conventions are:

- **Signals:** asynchronous business events, review messages, and approval decisions where durable receipt is more important than an immediate workflow response.
- **Updates:** operator commands such as approve, reject, request revision, correct fact, or take over when synchronous workflow validation and a returned result are required.
- **Queries:** read-only process status, pending approvals, and current wake conditions.
- **Durable Timers:** approval expiry, escalation deadlines, and follow-up timeouts.

The browser never connects directly to Temporal. The API authenticates and authorizes the human, writes each review message, decision, and audit entry to PostgreSQL, and uses an outbox to deliver the Signal or Update. The workflow verifies that the review thread and proposal revision are still current and that any decision applies to the exact action before execution.

The workflow's current state is authoritative for whether an approval can be applied, while PostgreSQL is authoritative for the immutable approval request and human-decision audit. This keeps approvals visible, queryable, testable, and independent of a specific agent SDK representation. An SDK `RunState` or interruption may be recorded for debugging or evaluated in the integration spike, but it is never the only durable approval state.

## 8. Multi-tenancy strategy

### Recommended initial model

Use a shared application schema with:

- Mandatory `tenant_id` on every tenant-owned record
- PostgreSQL row-level security with `FORCE ROW LEVEL SECURITY` on tenant tables
- Tenant-scoped foreign keys and unique constraints
- Non-owner application roles without `SUPERUSER` or `BYPASSRLS`
- A transaction-scoped tenant identity set with `SET LOCAL` on every database transaction
- Connection-pool reset checks so tenant context cannot leak between requests or jobs
- Separate migration, API, worker, and restricted support roles
- Automated tests attempting cross-tenant reads and writes
- Per-tenant quotas, rate limits, budgets, and audit trails

Do not use one PostgreSQL schema per client initially. Schema-per-client would significantly complicate Alembic migrations, pooled connections, analytics, onboarding, and operations.

Dedicated databases or deployments can be offered later for clients with regulatory, contractual, residency, or high-volume isolation requirements.

Use one Temporal namespace per environment initially. Tenant IDs should be represented in workflow IDs, search attributes, application records, and logs without exposing customer PII. Each immutable client-pack release derives its own task queue inside that namespace; dedicated namespaces remain an optional stronger operational boundary.

## 9. Process configuration

Processes should be configurable but not arbitrary executable workflows in the first release.

An immutable process-definition version should describe:

- Trigger events
- Applicable journey/case type and supported external resource types
- Goals and terminal states
- Agent instructions and prompt version
- Allowed tools and actions
- Allowed wake event types
- Default timers and SLA bounds
- Approval requirements
- Review roles, permitted interaction commands, and revision limits
- Escalation policy
- Memory and retention policy
- Model and reasoning configuration
- Integration bindings
- Owning public example or private client-pack ID and version
- Tenant-editable settings
- Definition version and compatibility metadata

The recommended authoring path is the opinionated, code-first project framework accepted in ADR-012. A client defines a `Project` containing `Journey`, `Route`, `Capability`, `Fact`, and `Scenario` objects. Tiramisu derives the low-level extension manifest, process definitions, action bindings, policy identities, business metadata, and strict OpenAI output schema into an immutable `ClientPack`. `tiramisu startproject`, `check`, `describe`, and `simulate` provide the conventional setup, inspection, and deterministic acceptance-test path. Simulation bindings are explicit and fail closed so author tooling cannot call a production provider accidentally. Direct `ClientPack` construction remains an advanced escape hatch. Generic examples live in the public repository; real client projects live in their private editable packages. The client UI should initially expose only safe, explicitly editable settings. A visual workflow builder is deferred until several real client implementations show which abstractions are stable.

Definitions follow `DRAFT → VALIDATED → EVALUATED → APPROVED → PUBLISHED → RETIRED`. Publication requires schema validation, deterministic scenario tests, agent evaluations, permission review, and compatibility checks.

Version these dimensions independently:

- Temporal workflow code and worker build
- Process definition
- Prompt and tool schema
- Model and reasoning configuration
- Event and memory schema
- Permission and communication policy
- Integration adapter and provider contract
- Tiramisu distribution, client pack, and extension-manifest compatibility

Active instances remain on compatible versions they started with. Migration to a newer version must be explicit, validated, and audited. Platform safety restrictions, tenant suspension, credential revocation, and emergency kill switches are live controls that may become stricter for existing instances. They cannot silently expand an existing instance's authority. Workflow-code upgrades use Temporal worker versioning and tested Continue-As-New boundaries rather than relying only on process-definition pinning.

## 10. Initial data model

The initial model is organized as conceptual aggregates; exact tables should follow the reference vertical slice instead of prematurely reproducing every client domain:

- **Tenancy and identity:** `tenants`, `tenant_users`, `customers`, roles, consent, and communication preferences.
- **Definitions and versions:** process definitions and immutable versions, prompts, policies, schemas, and publication metadata.
- **Journey execution:** `process_instances`, current workflow/run references, status, version pins, budgets, and lifecycle timestamps. A separate `agent_instances` table is added only if the one-to-one identity needs an independent lifecycle.
- **External correlations:** provider/resource references and relationship records linking enquiries, bookings, payments, calendar events, message threads, CRM records, and client-domain IDs to a process instance.
- **Event ingress:** webhook receipts, canonical event inbox, quarantine records, delivery attempts, and projection watermarks.
- **Agent history and memory:** turns, context snapshots, typed memory items, provenance, commitments, and compaction lineage.
- **Wake state:** event subscriptions, timers, cancellation, takeover, and approval waits.
- **Actions and review:** action requests with proposal lineage, policy decisions, review threads and attributed messages, approval requests and decisions, execution attempts, provider references, reconciliation, and compensation.
- **Integrations:** encrypted connections, tenant bindings, adapter version, and health status.
- **Deployment composition:** installed public package, private client-pack versions, extension-manifest hash, compatibility results, and worker build.
- **Delivery and audit:** outbox messages, immutable audit entries, usage, cost, and security events.

Avoid a generic JSON/EAV `business_objects` store unless a concrete client requirement justifies it. The platform should correlate to authoritative client/provider objects rather than attempting to become every client's booking, CRM, payment, or claims database. Detailed relationships, indexes, RLS policies, retention, erasure behavior, and partitioning will be designed before the first Alembic migration.

## 11. Reliability and safety requirements

### Delivery and side effects

- Every inbound event and operator command has a tenant-scoped deduplication key.
- Every external action has a stable idempotency key and a versioned, action-specific retry policy.
- Webhook persistence and workflow notification use a transactional inbox/outbox or an equivalent recoverable boundary.
- An Activity timeout does not imply provider failure. Unsafe retries wait for reconciliation when success is ambiguous.
- Action attempts record provider request references, returned resource IDs, errors, retry classification, and final reconciliation evidence.
- Payment providers and client business systems remain authoritative for their domain facts.
- Projection lag and failed outbox delivery are measurable and repairable through reconciliation jobs.

### Deterministic safety and bounded autonomy

- Sensitive actions require deterministic policy checks and, where configured, human approval.
- Missing, invalid, unavailable, or incompatible policy/configuration fails closed for mutating actions and escalates visibly.
- Approval applies only to the exact action payload reviewed, expires according to policy, and is revalidated before execution.
- Agent output is structured, schema-validated, and checked against current process state before use.
- The platform enforces maximum agent turns per wake, actions per period, follow-ups, timer horizons, process lifetime, token usage, and monetary cost.
- Tenant and platform circuit breakers can pause model calls, outbound communication, a capability, an integration, or all autonomous execution.
- Operators can pause, resume, cancel, message, approve, reject, reconcile, and take over an instance.
- Rate limits and budget exhaustion lead to an explicit waiting, escalation, or failed state; they do not cause unbounded retry loops.

### Communication policy

- Customer-facing communication respects recorded consent, opt-out, channel preference, locale, timezone, quiet hours, and frequency limits.
- Bounce, delivery failure, out-of-office, auto-responder, and reply-loop detection prevent autonomous message storms.
- Recipients and conversation threads are verified before sending sensitive or consequential information.
- Cancellation, unsubscribe, complaint, and legal-hold events can pre-empt ordinary follow-up work.

### Data protection and model safety

- Temporal workflow payloads contain opaque identifiers and minimal orchestration state where possible; Activities load sensitive content only when needed.
- Temporal Payload Codec encryption and a failure converter protect payloads and failure details in environments that require application-layer encryption.
- Customer PII and secrets do not enter Temporal search attributes, workflow IDs, logs, metrics, traces, or model context without a documented need.
- OpenAI storage, tracing, data residency, retention, redaction, and deletion behavior are configured per tenant and environment. Provider-managed conversation state is not assumed.
- Every email, attachment, document, webhook payload, external description, and operator review message is treated as untrusted input that may contain prompt injection.
- Provider credentials are never exposed to the model. Tools expose the least capability and data required for the current process stage.
- Input/output filtering, attachment controls, stable pseudonymous safety identifiers where appropriate, and adversarial prompt-injection tests are part of the safety baseline.
- Retention and erasure policy accounts for Temporal's durable history: raw deletable content is kept out of history where possible and referenced by opaque ID.

### Versioning, replay, and operations

- Process, prompt, tool-schema, model, policy, event-schema, memory-schema, adapter, and worker-build versions are included in every agent turn and action audit.
- Long workflows use Continue-As-New before history becomes excessive, with all pending waits, correlations, approvals, budgets, and mailbox state preserved.
- Workflow replay tests run in CI before workflow-code changes are deployed.
- Worker deployment/versioning and rollback are tested independently from process-definition migration.
- Structured logs, traces, and metrics correlate tenant, process, workflow/run, event, agent turn, action attempt, provider request, and model trace without leaking sensitive content.
- Initial service indicators include event-to-wake latency, mailbox age, pending-approval age, outbox lag, reconciliation backlog, unknown action outcomes, retry volume, stuck workflows, model tokens/cost, and follow-up-loop counts.

## 12. Testing strategy

Testing is designed around an integration-free kernel, provider contracts, deterministic Temporal orchestration, and a separate layer of probabilistic agent evaluation.

Current baseline: 243 backend tests (153 unit/contract, 86 PostgreSQL or Temporal integration, and 4 committed-history replay cases), 3 standalone support-project cases, 10 Vue component cases across 3 files, and 2 live-stack Playwright journeys. The strongest coverage is delivery races, exhaustive tenant-table RLS/grant enforcement, exact approval/action fencing, typed provider-conflict recovery, deterministic process-local communication/lifetime safety, durable model token/cost ledger with pre-call fencing and breaker enforcement, tenant Activity authorization, deterministic Temporal race ordering, workflow restart/rollover, manual reevaluation, interventions, generated client-project contracts, and identical compiled safe-adapter scenarios through both shared kernel transitions and the real PostgreSQL/Temporal path. Configuration evolution, agent behavior, cross-process consent, platform spend aggregation, broader adapter contracts, browser failure paths, security automation, and load/resilience remain material gaps. [`docs/testing.md`](docs/testing.md) is the actionable coverage map and ordered gap plan.

### Layer 1 — Pure kernel tests

Run without Temporal, PostgreSQL, OpenAI, network access, or provider SDKs. Provide process state, canonical events, scripted agent decisions, policy configuration, budgets, communication rules, operator review messages, and a fake clock. Assert state transitions, action decisions, approval requirements, proposal revision lineage, wake conditions, memory provenance, ordering, limits, and terminal outcomes.

These tests should be fast enough to cover a large matrix of process states and policy combinations on every change.

### Layer 2 — Temporal workflow tests

Run the real workflows using Temporal's test environment with:

- Scripted agent/model outcomes
- Mock Activities with production signatures
- Stub integration adapters
- Automatic or manual time skipping
- Injected Signals, Updates, cancellations, retries, and races

This layer verifies durable waiting, approval handling, timer/event races, retries, Continue-As-New, and workflow recovery without calling real providers.

It also verifies single-flight reasoning, mailbox ordering, review messages arriving while approval or an agent turn is pending, revision/supersession, cancellation/takeover priority, stale approval revalidation, and handler completion before workflow exit or Continue-As-New.

### Layer 3 — Agent behavior evaluations

Use the real OpenAI model with only stubbed business tools. Evaluate whether the agent chooses the correct capability, supplies valid arguments, respects permissions, requests missing information, responds appropriately to human suggestions and denied, failed, or ambiguous actions, produces a clearly revised proposal without silently executing it, selects valid wake conditions, distinguishes facts from inference, and maintains commitments across turns.

Agent evaluations use scored expectations and invariants rather than exact prose comparisons. Include adversarial emails and documents, conflicting customer claims, stale summaries, social-engineering attempts, and attempts to exceed action or communication limits. Model, prompt, tool, policy, and process-definition versions are captured with every result. Publishing compares results against an approved baseline and explicit safety thresholds.

### Layer 4 — Adapter contract tests

Every real and stub implementation of a port must pass the same provider-neutral contract suite. Calendar tests cover availability, time zones, daylight-saving transitions, conflicts, idempotent creation, updates, cancellation, timeout-after-success, and lookup-before-retry. Messaging tests cover sending, replies, bounces, out-of-office responses, duplicates, threading, opt-out, and ambiguous delivery. Payment tests cover requests, completion, failure, expiry, refund policy, duplicate webhooks, ambiguous responses, and reconciliation.

This prevents stubs from behaving more conveniently than production providers.

The same public suite validates private client packs without requiring them in public CI. It checks extension-manifest compatibility, process-definition publication, capability registration, policy monotonicity, adapter behavior, replay fixtures, migration compatibility, and that supported registrations do not replace core safety boundaries. It detects accidental contract violations; it does not sandbox deliberately malicious Python. The public repository also includes a bundled fictional pack as its local reference composition.

### Layer 5 — Provider sandbox tests

Run a smaller suite against real provider test environments and dedicated accounts. This verifies credentials, OAuth scopes, webhook signatures, provider mappings, rate limits, and behavior not represented by the port contract.

### Layer 6 — Replay and failure-injection tests

Replay representative Temporal histories in CI before workflow changes are deployed. Add explicit failure scenarios, including:

- Worker failure after a provider accepts an action but before the Activity records success
- Duplicate initiating events and provider webhooks
- Unmatched, ambiguous, conflicting, and late external correlations
- Approval arriving twice or after expiry
- Approval for a stale or changed action payload
- Revision feedback superseding a pending approval, followed by late approval of the old revision
- Multiple review messages arriving before, during, and after the revision turn
- Operator chat and a customer event changing relevant state at the same time
- Customer reply and follow-up timer becoming ready together
- Events, cancellation, and manual takeover arriving during an agent turn
- Provider success followed by a timeout or ambiguous response
- Provider without idempotency support requiring lookup-before-retry
- Continue-As-New with pending wake conditions or approvals
- Provider outage followed by recovery
- Budget, rate, follow-up, and process-lifetime limits being reached
- Out-of-office and auto-responder loops
- Prompt injection in emails, attachments, provider fields, and operator notes
- Tenant context reuse through pooled API and worker database connections
- Deletion of externally stored content referenced by immutable workflow history

The optional Temporal/OpenAI SDK integration spike must replay recorded histories and prove idempotent conversation/session continuation across Activity retries before it can replace the proposal-only Activity topology.

### Reusable test kit

Create a first-class process-testing package containing:

- `ScriptedAgent`
- `StubCalendarProvider`
- `StubMessagingProvider`
- `StubPaymentProvider`
- `StubBookingProvider`
- `FakeClock`
- `EventInjector`
- `CorrelationDriver`
- `ApprovalDriver`
- `OperatorReviewDriver`
- `ReconciliationDriver`
- `ScenarioRunner`
- Assertions for events, correlations, actions, attempts, review messages, proposal lineage, waits, approvals, budgets, memory provenance, reconciliation, and audit entries

Stubs emit the same canonical events as real adapters. A scenario should be reusable across pure kernel tests, Temporal tests with stubs, and selected provider sandbox tests.

Example scenario:

```text
Given an enquiry has been received
And the stub calendar has no availability this week
When the agent offers next week's slots
And no customer response arrives
And virtual time advances by 48 hours
Then one follow-up message is sent
And the agent waits for a reply or the next configured timeout
```

## 13. Suggested repository layout

The public repository begins as one editable Python package in a monorepo:

```text
.
├── PLAN.md
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md
├── THIRD_PARTY_NOTICES.md
├── compose.yaml
├── pyproject.toml
├── backend/
│   ├── alembic/
│   ├── src/tiramisu_agents/
│   │   ├── api/
│   │   ├── core/
│   │   │   ├── domain/
│   │   │   ├── ports/
│   │   │   └── contracts/
│   │   ├── temporal/
│   │   │   ├── activities/
│   │   │   └── workflows/
│   │   ├── agents/
│   │   ├── event_routing/
│   │   ├── db/
│   │   ├── adapters/
│   │   │   ├── providers/
│   │   │   └── stubs/
│   │   ├── builtin/
│   │   ├── policies/
│   │   ├── approvals/
│   │   ├── reconciliation/
│   │   ├── extensions/
│   │   ├── projects/
│   │   ├── security/
│   │   ├── observability/
│   │   └── testkit/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── contracts/
│       ├── evaluations/
│       ├── replay/
│       └── end_to_end/
├── frontend/
│   ├── src/
│   └── tests/
├── examples/
│   └── support_client_pack/
├── docs/
│   ├── architecture/
│   └── decisions/
└── infra/
```

An actual client pack lives in a different private repository:

```text
tiramisu-client-acme/
├── pyproject.toml
├── src/acme_tiramisu/
│   ├── project.py
│   └── adapters/
├── tests/
│   ├── contracts/
│   ├── scenarios/
│   └── evaluations/
└── deploy/
```

The product and public repository use the name **Tiramisu**. Initial development uses the import namespace `tiramisu_agents` and an editable local installation managed from the monorepo with `uv`. PyPI publication and the final distribution name are deliberately deferred. The unqualified `tiramisu` distribution name is already occupied and must not be assumed available if publication is considered later.

The exact packaging boundary remains intentionally simple until the first vertical slice identifies genuinely reusable components. Initially, the core, Temporal integration, API, public adapters, and test kit form one editable package. Local client-pack development uses a sibling editable path or workspace dependency; CI and deployments pin an exact Git revision or immutable build artifact. No PyPI release is required. Once the extension API is stable, decide whether to publish one distribution or split reusable provider adapters according to their dependency and release lifecycles.

## 14. Delivery roadmap

### Phase 0 — Product and architecture decisions

- [x] Select enquiry-to-booking as the first fictional reference customer journey.
- [x] Define the initial autonomous versus approval-required actions in versioned process policy.
- [x] Define permission-gateway outcomes and exact-payload approval integrity rules.
- [ ] Decide production Temporal deployment model.
- [x] Confirm shared-schema PostgreSQL RLS as the initial tenancy isolation model.
- [x] Confirm the stub-first integration sequence, with email as the likely first real provider afterward.
- [ ] Define the initial operator and client-admin experience.
- [x] Define initial canonical event, action, wake-condition, review-command, and agent-decision contracts.
- [x] Define the initial provider-neutral action port and explicit adapter registry contracts.
- [ ] Threat-model tenant isolation, webhooks, prompts, tools, and credentials.
- [x] Define minimum autonomy, communication, cost, and process-lifetime budgets. Conservative
  process communication/rate/lifetime/model-token/model-cost defaults and hard upper bounds are
  now compiled from client journeys, with a durable per-attempt usage ledger, tenant spend
  auto-trips, manual tenant/capability/outbound breakers, and deployment platform kill
  switches; cross-process consent, operational quotas, and platform spend aggregation remain.
- [x] Decide how client packs, tenants, API deployments, worker task queues, provider credentials, upgrades, and rollbacks map to one another: one immutable deployment and Temporal task queue per client pack, or per intentional identical-pack tenant group (ADR-011).
- [x] Confirm MIT licensing, public contribution policy, and the public/private client-pack boundary.

The following architecture decision records are gates for the durable kernel:

- [x] **ADR-001:** Journey/case aggregate, stable workflow identity, and external correlation.
- [x] **ADR-002:** Proposal-only agent Activity for the MVP and acceptance criteria for the Temporal/OpenAI integration spike.
- [x] **ADR-003:** Temporal/PostgreSQL/provider authority, inbox/outbox delivery, projections, and reconciliation.
- [x] **ADR-004:** Application-owned conversation history, memory provenance, sensitive-data handling, retention, and deletion.
- [x] **ADR-005:** Action idempotency, retry limits, ambiguous outcomes, reconciliation, and compensation.
- [x] **ADR-006:** Shared-schema RLS roles, tenant context, Temporal payload encryption, and isolation testing.
- [x] **ADR-007:** Version pinning, worker deployment, Continue-As-New, migration, and live safety overrides.
- [x] **ADR-008:** Mailbox ordering, concurrency, late events, cancellation, and timer/event ties.
- [x] **ADR-009:** Autonomy budgets, communication policy, circuit breakers, and operator takeover.
- [x] **ADR-010:** Public MIT monorepo, private client-pack boundary, packaging, extension manifest, compatibility, and release policy.
- [x] **ADR-011:** One independently deployable API/worker composition and Temporal task queue per client pack or identical-pack tenant group.

### Phase 1 — Project foundation

- [x] Scaffold the Python API and worker package boundaries.
- [x] Scaffold the Vue application.
- [x] Initialize the public repository with MIT `LICENSE`, `README`, contribution, security-reporting, code-of-conduct, and third-party notice files.
- [ ] Add secret scanning, dependency updates, lockfile review, SBOM generation, and safe CI behavior for untrusted public pull requests.
- [x] Establish the `tiramisu_agents` namespace, editable `uv` package workflow, internal compatibility versioning, and initial extension-manifest contract without requiring PyPI publication.
- [x] Bundle the fictional project declarations and bindings as the local reference composition, with its manifest, definition, and output contract generated through the public compiler.
- [x] Add PostgreSQL Compose service, SQLAlchemy models, and a reversible, drift-checked initial Alembic migration.
- [x] Add and runtime-validate the local Temporal development service.
- [x] Add forced PostgreSQL row-level security, composite tenant foreign keys, transaction-scoped tenant context, and separate local admin/runtime roles.
- [x] Establish initial tenant-aware API authentication with hash-only, scoped, expirable and revocable deployment credentials. Managed external identity-provider/browser sessions and production database role provisioning remain.
- [x] Add canonical event inbox, transactional outbox, external correlation registry, and quarantine persistence foundations.
- [x] Add a tenant-allow-listed Temporal outbox delivery worker with recoverable claims, bounded retries, explicit dead letters, attributed/idempotent requeue, recovery history, and idempotent Signal-With-Start delivery.
- [x] Add operator-driven quarantine resolution and replay. Tenant-scoped inspection, immutable attributed resolution, optional late reference binding, atomic original-event dispatch, and operator UI/history are implemented.
- [x] Add action-request proposal lineage, review-thread/message, approval, attempt, unknown-outcome, and reconciliation foundations, including exact action-result provenance and immutable evidence-backed operator resolution.
- [ ] Add autonomy budgets, communication policy, and rate limits. Process-local opt-out,
  automated-response suppression with genuine-reply reset, local quiet hours, durable rolling and
  total message reservations, follow-up count/spacing, and maximum process lifetime are enforced at
  proposal and provider boundaries. An audited tenant suspension control and hard semantic
  data/context limits are also in place, as are durable process token/cost budgets,
  tenant spend auto-trips, and manual tenant/capability/outbound breakers with operator
  trip/reset; cross-process consent, operational throughput quotas, and platform spend
  aggregation remain.
- [ ] Add data classification, log/trace redaction, Temporal payload encryption hooks, and retention configuration.
- [ ] Add explicit byte/count/token ceilings for ingress events, facts, action parameters, review context, commitments, and rendered model context. Hard semantic byte/count maxima, pre-persistence validation, prospective fact/context preflight, pre-provider prompt checks, durable model token/cost budgets, and fail-closed intervention are complete; raw HTTP body/attachment controls, provider-response limits, and tenant-specific lower ceilings remain.
- [x] Add formatting, linting, strict static typing, unit tests, dependency lockfiles, and CI.
- [ ] Add correlated structured logging, tracing, metrics, health checks, and initial stuck-work alerts.
- [x] Add the reusable stub providers and scenario test kit.

### Phase 2 — Durable agent kernel

- [ ] Implement `AgentWorkflow`. The process mailbox now sequences bounded event, timer, review, control, and action-result turns; executes or defers proposals; persists effective lifecycle/wake outcomes and durable interventions; performs immediate lookup-only reconciliation; rolls history over at safe Continue-As-New boundaries; and exposes turn/pending-action state. Delayed reconciliation schedules and mature production failure operations remain.
- [x] Enforce process-pinned deployment-release, task-queue, client-pack/manifest, and definition compatibility before model or provider I/O. Process creation now pins the logical deployment, immutable release and derived queue plus the canonical complete-pack fingerprint, manifest hash, and definition fingerprint; mismatches stop before external I/O and create an operator-visible intervention. Historical rows are marked unverified and fail closed pending an audited migration.
- [x] Implement the initial deterministic process mailbox with event deduplication, replaceable event/timer wake plans, state queries, and time-skipping tests.
- [x] Implement canonical event ingestion and source-event deduplication.
- [x] Implement exact correlation, quarantine-on-ambiguity, transactional outbox creation, and safe Signal-With-Start routing.
- [x] Implement quarantine resolution, late correlation, and replay. Existing reference ownership is preserved; concurrent resolutions and repeated commands deduplicate, and terminal destinations retain record-only behavior.
- [ ] Implement the single-flight mailbox, event priority, coalescing, and timer/event race handling. Automatic single-flight event, timer, priority-review, action-resolution, and exactly-once logical manual-reevaluation turns are complete; reserved manual wakes supersede the old plan and precede ordinary matching events/timers. The core event/timer, lifecycle-control/turn, review/turn, action-result/event, manual-wake, and Continue-As-New tie rules now have live race coverage. Explicit event batching and richer cancellation semantics remain.
- [x] Implement the bounded, proposal-only OpenAI Agents SDK Activity, strict output transport, at-most-two validator-guided semantic corrections against one trusted snapshot, bounded PostgreSQL event/review/action-result context loader, deterministic scripted-runner path, and automatic workflow consumption through the action gateway.
- [x] Implement bounded context assembly, sourced authoritative-fact/customer-claim projection, provenance-checked summaries and commitments, immutable state revisions, and deterministic lifecycle projection.
- [ ] Implement application-owned conversation/message history, memory compaction, and compaction lineage.
- [x] Implement initial typed decision validation for exact event lineage, allowed actions and wake events, per-turn action limits, and timer bounds.
- [x] Implement the initial action permission gateway with fail-closed classification, idempotent proposal persistence, exact approval commands, pre-execution revalidation, and execution routing. Budgets and live safety overrides are tracked separately.
- [x] Implement deterministic policy evaluation and durable exact-payload approvals.
- [ ] Implement durable review threads, revision/supersession, bounded operator-agent turns, approval Signals, operator Updates, and status Queries. Durable threads, attributed messages, exact approve/reject transitions, row-lock serialization, idempotent commands, supersession requests, transactional outbox Signals, bounded review context, replacement-turn provenance, and automatic workflow turns are complete; operator Updates and richer Queries remain.
- [ ] Implement action attempts, bounded retries, idempotent execution, ambiguous outcomes, and reconciliation. Durable attempts, stable payload-bound idempotency keys, exact approval revalidation, autonomous/approved stub execution, lookup-only automatic reconciliation, typed and size-bounded conflict outcomes, crash-safe conflict lookup, unchanged-conflict re-proposal rejection, action-result turns, and evidence-backed operator resolution are complete; delayed/background schedules, multi-attempt policy, backoff, backlog operations, and compensation remain.
- [ ] Implement the tenant integration registry and provider bindings. An explicit in-memory action-type registry and provider-neutral adapter contract are complete; tenant-configured bindings and credential resolution remain.
- [ ] Implement budget, communication-policy, and safety-boundary enforcement. The compiled
  client-pack contract now classifies outbound, genuine-reply, opt-out, and automated-response
  types. Process-local consent/loop suppression, local quiet hours, durable message reservations,
  rolling/total/follow-up limits use one pure evaluator in scenarios and at proposal/final provider
  boundaries; process lifetime additionally blocks model calls. Deployment-tenant authorization,
  lifecycle fencing, approval expiry, tenant suspension, and semantic data/context ceilings are
  also enforced, as are pre-model-call token/cost/tenant-spend fences with idempotent
  per-attempt recording, manual breaker enforcement at reservation and provider boundaries,
  and deployment platform kill switches; raw transport limits, cross-process consent,
  operational quotas, and platform spend aggregation remain.
- [ ] Implement Continue-As-New with complete mailbox, wait, version, approval, and budget carry-forward. Versioned rollover now preserves active mailbox buffers, delivery deduplication, recent diagnostics, pending approvals, absolute timers, process-definition identity, and lifetime counters; model spend needs no carry-forward because the PostgreSQL ledger is keyed by process, not by workflow run.
- [x] Add Temporal replay and failure-recovery tests. Committed signal/wait and Activity-backed Continue-As-New histories replay in CI; worker restart, rollover carry-forward, and retry-isolation tests cover the initial recovery surface. The broader failure matrix in the testing strategy remains ongoing.
- [ ] Add committed replay histories for approval/revision, reconciliation, intervention/retry, takeover, suspension, and terminal closure, plus the timer/event, control/turn, review/turn, and event/action-result race matrix. Reserved manual-wake ordering and takeover-during-turn histories are now committed alongside the original and Continue-As-New histories; the complete matrix also runs live against the time-skipping server.
- [ ] Run the optional Temporal/OpenAI SDK integration spike and record the decision without blocking the proposal-only path.

### Phase 3 — Reference journey

Recommended initial journey:

> Website enquiry → email conversation → proposed booking → booking confirmed → payment requested → payment completed → calendar updated → post-service follow-up → completed

- [x] Add fictional website enquiry ingestion through the development canonical-event route.
- [x] Add provider-neutral inbound/outbound messaging primitives with a stateful stub adapter.
- [x] Add provider-neutral availability and booking primitives with a stateful stub adapter.
- [x] Add provider-neutral payment request/completion primitives with a stateful stub adapter.
- [x] Add provider-neutral calendar primitives with a stateful stub adapter.
- [x] Add validated follow-up timers to the process mailbox.
- [x] Add manual approval, conversational revision, rejection, expiry, and takeover paths using the test driver and a minimal API surface.
- [x] Run the same compiled complete journey through both the integration-free kernel and the full PostgreSQL + Temporal path with stub messaging, booking, payment, and calendar providers.
- [ ] Run agent behavior evaluations against the same stubbed journey.
- [x] Demonstrate recovery after fresh worker and Activity compositions restart at the reference journey's review/wait boundaries.
- [ ] Demonstrate quarantine resolution, ambiguous provider reconciliation, opt-out, message-loop prevention, and budget exhaustion. Focused kernel/PostgreSQL tests now demonstrate communication suppression and message/lifetime exhaustion; authored full-journey negative scenarios, ambiguous reconciliation demonstrations remain; quarantine resolution now has a live browser journey and PostgreSQL/Temporal recovery coverage.
- [ ] Only after the stubbed journey passes, add one real provider integration and run the shared contract and sandbox suites.

### Phase 4 — Configurable client processes

- [x] Add immutable process-definition contracts, validation, fingerprinting, trigger resolution, and deterministic policy/instruction compilation.
- [x] Add the opinionated `Project`/`Journey`/`Route`/`Capability`/`Fact`/`Communications`/`Scenario` authoring framework, derived manifests and strict output schemas, `startproject`/`check`/`describe`, a migrated fictional journey, and a separately editable support example in CI (ADR-012).
- [ ] Add the process-definition draft, validation, evaluation, approval, publication, and retirement lifecycle.
- [ ] Add client-pack installation, compatibility validation, enable/disable, audit, and deployment composition. Explicit `module:attribute` loading from an installed/editable package, the validated public `ClientPack` contract, conventional author tooling, an independently authored editable-package example, shared API/worker composition, deterministic release identity/queues, and durable audited tenant assignment are complete. Persisted installation inventory, runtime enable/disable, provider credential resolution, ingress routing, and richer lifecycle controls remain.
- [x] Enforce published-only production process triggers and fingerprint-bound definition identities. Draft/retired definitions cannot install real triggers, and same-version behavior drift fails closed for active instances. An explicit draft simulation mode and audited active-instance migration remain separate work.
- [ ] Add tenant prompt and policy configuration.
- [ ] Add the tool and integration registry.
- [ ] Add safe client-editable settings.
- [ ] Add process simulation and validation before publication. Compiled scenarios now execute both without infrastructure and through a reusable PostgreSQL/Temporal driver using generated strict decision schemas, production decision/permission/action-identity rules, explicitly safe stub bindings, exact reviews, virtual time, durable audits, and shared fact/status/wake/completion behavior. Draft isolation, evaluation records, and publication gates remain.
- [x] Add the initial tenant process list/detail API and operator instance timeline, durable wake-condition, sourced-fact/claim, memory, and commitment UI.
- [ ] Add the full approval, proposal-diff, review-chat, revision-lineage, and manual-intervention UI. Exact-payload approve/reject/comment/request-revision and intervention retry/wake/takeover/resume controls are complete; Wake is explicitly presented as non-authoritative reevaluation. Diffs, complete thread history, expiry management, typed fact-correction controls, and richer intervention diagnostics remain.
- [ ] Add event-quarantine, unknown-action, and reconciliation UI. Event-quarantine inspection, resolution, and history are complete; unknown-action and reconciliation views remain.
- [x] Define active-instance migration behavior. Existing processes remain pinned to their immutable release while old/new workers coexist; tenant moves require terminal processes and published deliveries. Active-process pin migration is deliberately unsupported until an audited, replay-safe migration command is designed and implemented.
- [ ] Decide the public distribution name and registry strategy when the extension API is stable; only then publish signed/versioned Python distributions if useful. Container releases may proceed independently.

### Phase 5 — Production hardening

- [ ] Harden and load-test per-tenant usage limits, budgets, circuit breakers, and back-pressure.
- [ ] Add mature stuck-workflow, quarantine, reconciliation-backlog, and dead-letter operations. Explicit tenant-scoped dead-letter inspection, immutable requeue audit, fresh bounded attempt cycles, permissioned APIs, and a reasoned Vue recovery view with process links and history are complete; bulk operations, alerts, retention, and the other operational backlogs remain.
- [ ] Complete tenant-specific PII retention, deletion, legal-hold, residency, and audit controls.
- [ ] Add secret management and credential rotation.
- [ ] Expand agent quality, regression, adversarial, and safety evals.
- [ ] Add failure injection and load testing.
- [ ] Add shared adapter contract suites and provider sandbox tests. Initial reusable checks cover success idempotency, timeout-after-success recovery, definitive failure, lookup-recoverable definitive conflict, and hold expiry; malformed responses, rate limits, credential selection, domain suites, and real sandboxes remain.
- [ ] Add migration-from-previous-release, supported downgrade/upgrade, data-preservation, and full tenant-table RLS/grant audit gates. The conflict migration has an isolated populated downgrade/upgrade test; the runtime-role boundary is round-tripped in CI, and every mapped tenant table now has an exact policy/grant/filter/pool-context audit. Generalized data-bearing release fixtures remain.
- [ ] Expand Playwright beyond the single smoke to live review revision, stale proposal, dead-letter recovery, intervention, and partial-scope credential journeys.
- [ ] Add token and cost reporting per tenant and process.
- [ ] Add backup, disaster recovery, and audit export procedures.
- [ ] Document deployment and operational runbooks.

## 15. Reference journey acceptance criteria

The first vertical slice is complete when:

1. A new enquiry starts exactly one agent workflow.
2. Duplicate delivery of the initiating event does not create a second workflow.
3. The agent can send a message and wait for a reply or timeout.
4. An inbound reply wakes the same logical agent with the correct context.
5. The same stable process correlates the enquiry, message thread, booking, payment, and calendar resources without changing workflow identity.
6. An unmatched or ambiguous inbound event is quarantined rather than attached to a guessed process.
7. The agent can progress through booking, payment, calendar, and completion events.
8. A worker can restart during any wait without losing state.
9. Retried Activities do not duplicate external side effects; timeout-after-success enters reconciliation and resolves correctly.
10. High-risk actions pause for approval according to tenant policy, and stale approval never executes.
11. Budgets, communication frequency, quiet hours, opt-out, and process-lifetime limits are enforced outside the prompt.
12. Auto-responder and duplicate-event loops cannot produce an outbound message storm.
13. Authoritative facts, customer claims, agent inferences, and summaries remain distinguishable after compaction.
14. An operator can inspect the full event, correlation, decision, action-attempt, approval, reconciliation, and wake timeline.
15. One tenant cannot read, signal, correlate, configure, or operate another tenant's process, including through pooled worker connections.
16. Sensitive message content is not present in workflow IDs, Temporal search attributes, or unredacted logs and traces.
17. The workflow can Continue-As-New without losing logical identity, mailbox events, waits, correlations, approvals, budgets, or context.
18. The process can be completed, cancelled, or manually taken over cleanly.
19. The same scenario passes against the pure kernel and Temporal workflow with stub providers.
20. Real and stub adapters satisfy the same provider contract tests.
21. Approval, rejection, expiry, duplicate decision, and stale-payload paths are covered.
22. Multi-day timers can be tested through virtual time without waiting in real time.
23. An operator can comment or request a revision, and the same logical agent responds using the durable process and review context.
24. Revision feedback supersedes the original proposal; a late approval of the old revision cannot execute it.
25. The operator can compare proposal revisions and approve only the exact final action payload.
26. The public repository builds, tests, and runs the fictional reference journey without any private client package.
27. The bundled fictional project owns its journey, route, capability, fact, scenario, and binding declarations in one package; its definition, manifest, and strict output contract are compiler-generated. Downstream client packages use the same public project/compiler/factory path and pass the same contract suites.
28. The supported client-pack registration path cannot replace the workflow, action gateway, tenant checks, approval integrity, budgets, or audit path. Client-pack code is nevertheless a trusted executable artifact; isolation from malicious code is a deployment boundary, not a type-contract guarantee.
29. Repository and release checks detect committed secrets, unsafe fixtures, generated credentials, and prohibited client/customer content.
30. Wake and Resume each cause one durable reevaluation turn even when the current plan waits for another event; duplicate delivery and Continue-As-New do not repeat it.
31. Operator reevaluation guidance is visible to the agent but cannot change an authoritative fact; a real provider event or typed, audited correction is required.
32. A client implementer can scaffold, compile, and explain an independently installable project without hand-writing a manifest, policy registry, action union, or low-level process definition.
33. A process cannot complete until its declared authoritative completion facts match at both the proposal-validation and state-persistence boundaries.

## 16. Open decisions

### D-001: First reference journey

Recommended: enquiry-to-paid-booking.

Questions:

- What actual client or industry should shape this journey?
- What marks the process as successfully complete?

### D-002: Initial integrations

Recommended: implement the complete provider-neutral journey with deterministic stub messaging, booking, payment, and calendar adapters first. Add one real provider only after the kernel, failure scenarios, and shared adapter contract pass. Email is the likely first real integration because it proves correlation and durable reply handling; Stripe test mode follows once action reconciliation exists.

Questions:

- Which email provider should be used?
- Is there an existing booking or CRM platform to target?
- Should Google Calendar, Microsoft 365, or both be supported first?

### D-003: Autonomy policy

Recommended initial policy:

- Agent may autonomously draft routine email; sending is autonomous only within recipient, consent, quiet-hour, template/content, frequency, and follow-up limits.
- Agent may create payment links but not capture arbitrary charges or issue refunds.
- Booking creation and modification may be autonomous within explicit bounds.
- Cancellation, refunds, unusual discounts, and irreversible actions require approval.
- A reviewer may ask questions or request revisions in natural language, but any material change creates a new proposal revision requiring a fresh exact approval.

This must be confirmed before implementing real integrations.

### D-004: Tenant isolation

Recommended: shared application schema with row-level security, with dedicated databases offered only where required.

Status: Unconfirmed.

The hardened shared-schema/RLS design in this plan is the working default unless a reference client's contractual or regulatory requirements require stronger isolation.

### D-005: Temporal deployment

Recommended: local Temporal for development and Temporal Cloud for production unless self-hosting is a product requirement.

Status: Unconfirmed.

### D-006: Frontend scope

Recommended sequence:

1. Internal operator console
2. Client administration and safe configuration
3. Optional customer-facing portal or chat surface

Status: Unconfirmed.

### D-007: Process authoring

Decision: use version-controlled, code-first `Project`, `Journey`, `Route`, `Capability`, `Fact`, and `Scenario` conventions that compile to the stable low-level client-pack contract. Expose limited typed client-editable settings later; defer a visual builder.

Status: Accepted in ADR-012.

### D-008: Journey identity and correlation

Recommended: generate a stable platform process ID when the initiating event is accepted. Store enquiry, booking, payment, calendar, CRM, and message-thread IDs as external correlations. Never derive permanent workflow identity from a provider object that may not exist yet or may change.

Status: Recommended working default; confirm merge, split, reopen, and late-event rules for the reference industry.

### D-009: OpenAI/Temporal execution topology

Recommended: use the bounded proposal-only Agents SDK Activity for the MVP. Keep all mutating tools in the application action gateway and Activities. Evaluate Temporal's Agents SDK integration in a non-blocking spike against the acceptance criteria in ADR-002.

Status: Recommended working default.

### D-010: Conversation and memory state

Recommended: application-owned PostgreSQL history and generated context snapshots. Do not combine local replay with SDK Sessions, OpenAI Conversations, or `previous_response_id`. Revisit one alternative only if evaluation shows material cost, quality, or operational benefit.

Status: Recommended working default; tenant-specific OpenAI storage, tracing, residency, and retention requirements remain to be defined.

### D-011: Initial autonomy and communication limits

Recommended safe defaults for the stubbed reference journey:

- One reasoning turn at a time and a bounded number of model/tool iterations per wake.
- No more than one automated follow-up in 24 hours and three without a customer reply.
- Tenant-local quiet hours and immediate opt-out enforcement.
- No direct charges, refunds, cancellation, unusual discounts, or sensitive recipient changes without approval.
- Explicit per-process token/cost and maximum-lifetime budgets.

Exact numeric limits should be configuration with conservative platform maxima. They must be confirmed before any real customer communication.

### D-012: Data protection baseline

Recommended: use opaque IDs in Temporal history, application-layer Temporal payload encryption outside local development, redacted logs/traces, externally stored deletable message content, and per-tenant OpenAI storage/tracing controls.

Questions:

- Which jurisdictions and regulated data classes must the first deployment support?
- Are there tenant requirements for Zero Data Retention, regional processing, or customer-managed keys?

### D-013: Open-source and client-extension model

Recommended: publish the generic platform, generic Vue application, test kit, stubs, examples, and reusable provider adapters under MIT. Keep each real client's processes, prompts, proprietary policies, bespoke adapters, evaluations, and deployment composition in a separate private repository and package.

Recommended initial packaging:

- Public product and repository name: **Tiramisu**
- Initial installation: editable local package from the monorepo using `uv`
- Python import namespace: `tiramisu_agents`
- PyPI distribution name and publication: deferred until the extension API is stable and there is a concrete need
- One internal package boundary initially; future distribution splitting remains undecided
- Explicit versioned extension manifest composed at worker startup
- Bundled fictional client pack used as the local reference composition
- No canonical client implementation stored only in a gitignored directory

Status: Recommended working default. The unqualified `tiramisu` PyPI distribution is already occupied, but no replacement name needs to be chosen or reserved during the editable-package phase.

MIT is an intentional permissive choice: downstream users may use, modify, privately fork, redistribute, and commercially host the public code without publishing their changes, provided they retain the required license notice. The expected differentiation is managed operation, implementation expertise, support, client packs, and proprietary client integrations rather than preventing third-party hosted forks. If that tradeoff is not acceptable, the license must be reconsidered before the first public release rather than changed casually after outside contributions begin.

### D-014: Client-pack deployment topology

Recommended near-term default: one stable logical API/worker deployment per client pack, or per group of tenants intentionally sharing the exact same pack, adapter routing, model configuration, and release cadence. Every immutable build/pack/model/Tiramisu release derives a separate Temporal task queue. Keep shared-schema PostgreSQL RLS, but require both an explicit service allow-list and a durable audited tenant assignment. A future control plane may route tenants to deployments without dynamically importing tenant-selected Python in a running worker.

Why: the current process registry, strict output type, and action bindings are process-wide. Supporting different client packs inside one worker would require tenant-aware definition, model, adapter, credential, task-queue, compatibility, and rollout routing across every Activity. That complexity should be justified by operational evidence rather than assumed early.

Status: Accepted and implemented. Recorded in ADR-011 on 2026-08-31; release identity, tenant assignment, process pins, release-fenced dispatch, and rollout/rollback rules landed on 2026-09-01. Active-process migration remains intentionally unsupported.

## 17. Explicit non-goals for the first release

- A fully general no-code workflow builder
- Arbitrary code or predicates generated by the model
- Unlimited autonomous financial authority
- A separate application schema for every tenant
- Supporting every email, calendar, booking, CRM, and payment provider
- Using a full unbounded transcript as agent memory
- Treating OpenAI conversation storage as the business system of record
- Mixing local conversation replay with provider-managed continuation state without an explicit migration/reconciliation design
- Treating an SDK-internal interruption as the only durable approval record
- Letting provider-specific APIs or credentials leak into the agent kernel
- Guaranteeing exactly-once behavior from external services that only support at-least-once delivery
- Allowing model-generated summaries or inferences to overwrite authoritative business facts
- Automatically attaching ambiguous external events to the most likely process
- Allowing an agent to increase its own permissions, budgets, timer horizons, or communication limits
- Storing canonical client source, prompts, policies, evaluation data, or deployment configuration only in a gitignored public-repository directory
- Maintaining a separate fork of the Tiramisu core for each client
- Performing dynamic extension discovery or imports from inside Temporal workflow execution
- Allowing private extensions to introduce client-specific database migrations or bypass public safety contracts in the first release
- Treating trusted in-process Python extensions as sandboxed from the host application

## 18. Immediate next step

The initial executable milestone—fictional enquiry through booking, payment, calendar, and completion—is working through both an integration-free demonstration and the real PostgreSQL/Temporal path. ADR-011's release boundary now makes that foundation safe to evolve across rolling pack releases. The next milestone is:

1. Complete durable model token/cost accounting and budgets plus tenant/platform/capability circuit
   breakers. Per-process token/cost budgets with a durable per-attempt ledger, tenant spend
   auto-trips, manual tenant/capability/outbound breakers with operator trip/reset, and
   deployment platform kill switches are complete; shared cross-process consent,
   recipient-specific timezones, and platform spend aggregation remain explicit production
   messaging gates.
2. Quarantine resolution and replay with operator visibility are complete: original-event inspection, audited destination selection, optional late reference binding, retry-safe dispatch, and resolution history. See [`docs/event-quarantine.md`](docs/event-quarantine.md).
3. Add isolated draft simulation, evaluation records, and publication gates on top of the shared scenario drivers.
4. Add real-model evaluations and the shared messaging adapter contract before connecting a real email provider.
5. Expand committed operational histories and generalized data-bearing migration fixtures as described in [`docs/testing.md`](docs/testing.md).

D-001 (reference industry and completion criteria), D-003 (real-world autonomy), D-005 (production Temporal deployment), and D-012 (data/compliance requirements) remain explicit gates before production integrations. D-014 is implemented as ADR-011. Now that the communication safety envelope is in place, a GitHub-issue triage/Codex handoff pack remains a useful later validation of that boundary rather than the next product milestone.

## 19. Design references

- [OpenAI Agents SDK: running agents and conversation-state strategies](https://developers.openai.com/api/docs/guides/agents/running-agents)
- [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)
- [OpenAI safety best practices](https://developers.openai.com/api/docs/guides/safety-best-practices)
- [OpenAI production best practices](https://developers.openai.com/api/docs/guides/production-best-practices)
- [Temporal Python/OpenAI Agents SDK integration](https://github.com/temporalio/sdk-python/blob/main/temporalio/contrib/openai_agents/README.md)
- [Temporal retry policies](https://docs.temporal.io/encyclopedia/retry-policies)
- [Temporal data encryption](https://docs.temporal.io/production-deployment/data-encryption)
- [Temporal Python message passing](https://docs.temporal.io/develop/python/message-passing)
- [Temporal worker versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning)
- [Temporal Python SDK MIT license](https://github.com/temporalio/sdk-python/blob/main/LICENSE)
- [OpenAI Agents SDK MIT license](https://github.com/openai/openai-agents-python/blob/main/LICENSE)
- [Existing `tiramisu` distribution on PyPI](https://pypi.org/project/tiramisu/)
