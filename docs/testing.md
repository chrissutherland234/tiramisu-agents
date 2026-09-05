# Testing strategy and gap plan

Last reviewed: 2026-09-05

Tiramisu's tests must prove durable business invariants across boundaries, not merely achieve line coverage. The highest-risk failures happen between PostgreSQL, Temporal, model execution, operator commands, and external providers, where no shared transaction exists.

## Current baseline

The repository currently has:

- 243 backend tests: 153 unit/contract cases, 86 PostgreSQL or Temporal integration cases, and 4 committed-history replay cases.
- 3 tests in the independently installable support client project, run from its own locked editable environment.
- 10 Vue component cases across 3 files covering the operator journey/review/intervention/dead-letter/model-budget surface, permission degradation, polling, and Wake authority wording.
- 2 Playwright live-stack journeys covering real event ingestion, process inspection, takeover, resume, the delivery-operations shell, and quarantine resolution/history with late reference routing.
- CI gates for locked dependency installation, Alembic drift, Ruff, Pyright, backend tests with PostgreSQL, conventional project compilation and generated OpenAI schemas, the standalone client project's own lint/type/test checks, Python package builds, Vue unit/type/build checks, the Playwright smoke, Compose startup, PostgreSQL runtime-role access, and Temporal health.

Strong current coverage includes concurrent initiating-event deduplication, correlation persistence, audited quarantine resolution and late reference binding with atomic rollback, concurrent-command fencing, immutable original events, terminal record-only handling, and lost-response Temporal redelivery, outbox claim ownership and dead-letter recovery, exhaustive tenant-table RLS/grant enforcement and pooled-connection context reset, tenant-scoped credentials and Activities, exact-payload approvals, provider execution fencing, typed conflict lookup recovery and unchanged-conflict re-proposal rejection, pinned deployment-release/queue/pack/definition compatibility before model and provider I/O, audited tenant assignment, old/new release dispatch fencing, published-only triggers, byte/count boundaries for event/fact/review/proposal/conflict data, pre-model context and prospective-fact limits, pre-provider prompt limits, bounded semantic proposal repair with unchanged-snapshot and exhaustion checks, process-local opt-out/automated-response/quiet-hour/rate/follow-up/lifetime enforcement through a shared pure evaluator and durable provider fence, durable per-attempt model token/cost ledger with pre-call budget fencing and idempotent ledger writes and distinct spend for reexecuted Activities, tenant spend auto-trips, manual tenant/capability/outbound breakers with operator trip/reset API, intervention/retry controls, single-flight mailbox turns, worker restart, Continue-As-New, reserved manual reevaluation, deterministic event/timer, lifecycle-control/turn, review, action-result, and rollover races, generated capability and wake schemas, authoritative completion gates, populated migration downgrade/upgrade checks, and identical compiled client-project scenarios running through both the integration-free kernel and the PostgreSQL/Temporal stack with safe adapters, exact approvals, durable audit assertions, worker restarts, time-skipped timers, and step-specific failures.

The count is not itself a release signal. Agent evaluation, broader provider contracts, load behavior, security automation, historical release migrations, platform-wide spend aggregation, and committed replay coverage for several operational commands remain absent or shallow.

## Test layers and required gates

### 1. Contract and deterministic kernel tests

Run on every change without PostgreSQL, Temporal, OpenAI, or network access. Cover Pydantic contracts, author-facing project and low-level process-definition compilation, generated model schemas, policy, lifecycle invariants, context bounds, provenance, version compatibility, and adapter contracts.

Next additions:

- Add the full immutable definition publication lifecycle and an explicit isolated draft simulation mode.
- Add table-driven negative scenarios for provider conflicts, approval revision, intervention recovery, and invalid completion across both scenario drivers.
- Add table-driven coverage for every platform limit and future tenant-configured lower ceiling; current tests cover representative exact-byte, one-over, no-persistence, prospective-projection, and no-provider-I/O boundaries.
- Add table-driven lifecycle tests for every process status × action/review/control operation.
- Add generated/state-machine tests for decision provenance, logical action identity, and wake-plan invariants once the core transitions are factored into a genuinely infrastructure-free kernel.

The former fictional-only driver has been removed. `ScenarioRunner` validates scripted decisions through generated output and production decision policy, uses production permission and action-identity rules, invokes only explicitly safe simulation bindings, and shares fact/status/wake/completion transitions with `ProcessStateService`. `PostgresTemporalScenarioDriver` consumes the same immutable compiled steps and runs them through real ingestion, outbox dispatch, mailbox and Activity orchestration, review approval, provider execution, persistence, and Temporal time skipping. Its final audit checks the durable event/action/approval/fact/wake/turn/completion record, while its test tenant is isolated and removed by default. This covers the ordinary cross-layer acceptance path; draft publication isolation, richer failure scenarios, and model evaluation remain.

### 2. PostgreSQL and API integration tests

Run against the migrated least-privilege runtime role in CI. Verify transaction boundaries, row locks, constraints, RLS, API authorization, immutable audit records, and outbox behavior.

Next additions:

- Cover every credential scope and role against every operator endpoint, including read-only UI degradation.
- Expand event-ID/source-ID collision and bulk-backlog cases. Quarantine resolution now covers conflicting correlations, late reference assignment, immutable original-event replay, transaction rollback, concurrent resolution/ingress, authenticated tenant isolation, and lost-response delivery to the real Temporal mailbox.
- Exercise dispatcher backoff timestamps, exhaustion, concurrent requeue/claim, retention boundaries, and bulk backlog pagination.
- Generalize the populated migration fixture across each released schema boundary and representative business records. The conflict migration has an isolated data-bearing downgrade/upgrade test; CI round-trips both the runtime-role hardening boundary and migration 13 down to 12 on the main test database. The runtime role's grants, forced policies, non-owner flags, schema privileges, tenant filtering, and transaction-local pool context are exhaustively audited against every mapped tenant table.
- Add request-size and malformed-JSON limits before production ingress is exposed.

### 3. Temporal workflow and replay tests

Use the real time-skipping workflow environment with scripted Activities. Every workflow-code change must replay committed histories and add a fixture when it changes a durable command, snapshot, or ordering rule.

Priority race matrix:

| Concurrent conditions | Required invariant |
| --- | --- |
| Customer event and timer become ready together | Stable documented priority; neither source is lost |
| Takeover/cancel and agent turn overlap | No later model result or provider side effect escapes the lifecycle fence |
| Multiple review comments/revision/approval arrive around a turn | Deterministic order; stale payload never executes |
| Action result and customer event arrive together | Single-flight turns preserve both source lineages |
| Manual wake, matching business event, and old timer are ready | Review/resolution/control priority is retained; manual reevaluation runs next, clears the old plan, and the business event remains available for the replacement plan |
| Continue-As-New with each pending command type | Every buffer, dedupe key, absolute timer, approval, intervention, and counter survives |
| Old/new worker releases overlap | Each dispatcher claims only its process-pinned release queue; incompatible work fails closed before model or provider I/O |

The matrix above is covered by real time-skipping workflow tests and PostgreSQL serialization tests. Lifecycle controls now supersede an in-flight model result at the next durable Activity boundary; an external action already underway remains protected by the final PostgreSQL lifecycle fence. Review commands serialize at the database row lock, buffered review/action/control sources retain deterministic priority, and duplicated commands remain exactly once across Continue-As-New.

The committed histories cover the original signal/wait path, Activity-backed Continue-As-New, reserved manual-wake ordering, and takeover during an active model turn. Expand them further to include approval/revision, action reconciliation, intervention/retry, tenant suspension, and terminal closure. Release identity, queue derivation, and concurrent old/new dispatch are covered outside workflow history; add deployment rollback and drain tests to a future live deployment harness.

### 4. End-to-end operator tests

Keep Playwright small but meaningful. Seed state through supported test fixtures or APIs, not browser-only mocks.

Add live-stack cases for:

- Exact approval, suggestion/revision, old-proposal rejection, and final approval.
- A dead-lettered outbox message, reasoned requeue, disappearance from the queue, and retained history.
- Failed-turn intervention and retry, including process navigation from an operational item.
- Read-only and partial-scope credentials.
- Empty, loading, API failure, long error/payload, pagination, and mobile layouts.

Vue component tests should separately cover validation and API error states. Accessibility checks should be added before the console expands into client administration.

### 5. Agent behavior evaluations

No live-model behavior suite exists yet. Add an opt-in evaluation runner using the real OpenAI model with stub providers and no mutating external systems. Store versioned synthetic cases and scored outcomes, not exact prose snapshots.

Initial evaluation corpus:

- Happy enquiry-to-booking variants and missing-information turns.
- Schema-valid but policy-invalid proposals that should self-correct from deterministic feedback, including exhaustion cases that must require operator intervention.
- Human comments and revision suggestions that must create a new exact proposal.
- Conflicting customer claims versus authoritative provider facts.
- Operator Wake guidance that asserts an unsupported payment or other fact; the agent must retain the authoritative state and establish an appropriate new wake plan.
- Provider denial, definitive failure, ambiguous outcome, and delayed recovery.
- Prompt injection, social engineering, malicious provider text, and attempts to expand authority.
- Opt-out, quiet hours, repeated follow-up, out-of-office, auto-responder, and duplicate-message loops.
- Long-running memory/commitment continuity after compaction.

Publishing a definition or changing its prompt/model requires an approved baseline, zero hard-invariant violations, and explicit thresholds for task completion, unnecessary escalation, invalid proposals, and communication-policy breaches. CI should run deterministic eval fixtures on every change; live model evals may run manually/nightly until cost and flakiness are understood.

### 6. Shared adapter contracts and provider sandboxes

Create one public contract suite per provider-neutral port. Run the same suite against stubs, private adapters, and—where possible—provider sandboxes.

The public contract helpers now cover stable success idempotency, timeout-after-success recovery, definitive failure without an outcome, and lookup-recoverable definitive conflicts—including repeated execution with the same key. A definitive conflict lookup must return the same terminal conflict and must not cause a repeated side effect. Expand the suite with ambiguous failure before provider acceptance, malformed provider responses, rate limits, and tenant credential selection. Adapters that model resource holds must also cover expiry as either a success, a definitive conflict, or an explicitly ambiguous provider outcome. Domain-specific suites add email threading/bounce/opt-out, calendar time-zone/DST/conflict behavior, payment webhook duplication/expiry, and booking concurrency.

Exact approval freshness is intentionally an execution-gateway contract, rather than an adapter contract: the core rechecks approval immediately before dispatch and the PostgreSQL integration suite proves that an expired approval cannot cross that fence. The same suite proves lookup-only reconciliation for ambiguous outcomes. This separation lets private adapters share the public provider contract without receiving review or database authority.

Do not add a real provider until its stub passes the shared contract and the failure semantics are representable by the port.

### 7. Security, resilience, and load tests

Before a public deployment:

- Add secret scanning, dependency review/update automation, SBOM generation, and pinned CI action revisions.
- Threat-test tenant identifiers, credential parsing, oversized events, prompt-bearing fields, webhook replay, and RLS connection-pool reuse.
- Verify sensitive content is absent from logs, traces, workflow IDs, search attributes, test artifacts, and exception messages.
- Load-test concurrent process starts, mailbox signals, pending timers, review queues, outbox dispatch/requeue, PostgreSQL pool pressure, and noisy-neighbor tenant limits.
- Inject PostgreSQL, Temporal, model, and provider outages of different durations; assert backlog recovery, bounded retries, and operator visibility.
- Test backup restore, Temporal/PostgreSQL recovery-point mismatch, audit export, and disaster-recovery procedures.

## Near-term testing tranche

Implement these in order:

1. Live-stack Playwright coverage for review revision, dead-letter recovery, intervention, and partial scopes.
2. The first deterministic agent-evaluation corpus and shared messaging adapter contract.
3. Additional committed histories for approval/revision, reconciliation, intervention/retry, suspension, and terminal closure.
4. Generalized data-bearing migration fixtures across supported release boundaries.
5. Load, fault-injection, security, and provider-sandbox suites after communication safety and real integrations exist.

## Definition of done for a feature

A feature is complete only when its deterministic contract, persistence/authorization behavior, Temporal ordering and recovery behavior where applicable, operator visibility, failure path, and documentation are covered at the lowest useful layer. New workflow commands or snapshot fields require replay evidence. New provider side effects require shared adapter-contract evidence. New model behavior requires evaluation evidence. A happy-path browser test alone is never sufficient for a durable-agent feature.
