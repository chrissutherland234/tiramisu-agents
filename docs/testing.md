# Testing strategy and gap plan

Last reviewed: 2026-09-01

Tiramisu's tests must prove durable business invariants across boundaries, not merely achieve line coverage. The highest-risk failures happen between PostgreSQL, Temporal, model execution, operator commands, and external providers, where no shared transaction exists.

## Current baseline

The repository currently has:

- 122 backend tests: 83 unit/contract cases, 36 PostgreSQL or Temporal integration cases, and 3 committed-history replay cases.
- 5 Vue component cases across 2 files covering the operator journey/review/intervention/dead-letter surface, permission degradation, polling, and Wake authority wording.
- 1 Playwright live-stack journey covering real event ingestion, process inspection, takeover, resume, and the delivery-operations shell.
- CI gates for locked dependency installation, Alembic drift, Ruff, Pyright, backend tests with PostgreSQL, Python package builds, Vue unit/type/build checks, the Playwright smoke, Compose startup, PostgreSQL runtime-role access, and Temporal health.

Strong current coverage includes concurrent initiating-event deduplication, correlation persistence, outbox claim ownership and dead-letter recovery, tenant-scoped credentials and Activities, exact-payload approvals, provider execution fencing, pinned deployment-release/queue/pack/definition compatibility before model and provider I/O, audited tenant assignment, old/new release dispatch fencing, published-only triggers, bounded semantic proposal repair with unchanged-snapshot and exhaustion checks, intervention/retry controls, single-flight mailbox turns, worker restart, Continue-As-New, reserved manual reevaluation, and a complete scripted enquiry-to-booking journey that proves Wake guidance cannot manufacture completed payment.

The count is not itself a release signal. Agent evaluation, provider contracts, load behavior, security automation, migration upgrade paths, and several concurrency combinations remain absent or shallow.

## Test layers and required gates

### 1. Contract and deterministic kernel tests

Run on every change without PostgreSQL, Temporal, OpenAI, or network access. Cover Pydantic contracts, process-definition compilation, policy, lifecycle invariants, context bounds, provenance, version compatibility, and adapter contracts.

Next additions:

- Add the full immutable definition publication lifecycle and an explicit isolated draft simulation mode.
- Bound event payload bytes, fact counts and values, action parameters, review context, commitments, and rendered model context.
- Add table-driven lifecycle tests for every process status × action/review/control operation.
- Add generated/state-machine tests for decision provenance, logical action identity, and wake-plan invariants once the core transitions are factored into a genuinely infrastructure-free kernel.

The current `FictionalJourneyDriver` is useful as a provider/policy demonstration, but it implements a second simplified action lifecycle. It must not be treated as proof of the production action gateway. Replace or supplement it with a reusable scenario description whose assertions can drive both the production kernel services and the Temporal stack.

### 2. PostgreSQL and API integration tests

Run against the migrated least-privilege runtime role in CI. Verify transaction boundaries, row locks, constraints, RLS, API authorization, immutable audit records, and outbox behavior.

Next additions:

- Enumerate every tenant-owned table and prove forced RLS, the expected policy, runtime-role grants, and cross-tenant denial; the current direct RLS test samples only `tenants`.
- Cover every credential scope and role against every operator endpoint, including read-only UI degradation.
- Exercise conflicting correlations, event-ID/source-ID collisions, late reference assignment, quarantine resolution, and replay once that feature exists.
- Exercise dispatcher backoff timestamps, exhaustion, concurrent requeue/claim, retention boundaries, and bulk backlog pagination.
- Test migration from the previous released schema with representative data preservation in CI—not only an empty-database upgrade and autogenerate drift check. CI now round-trips migration 13 down to 12 and back on an empty database; the generalized populated previous-release fixture remains.
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

The committed histories cover the original signal/wait path, Activity-backed Continue-As-New, and reserved manual-wake ordering. Expand them further to include approval/revision, action reconciliation, intervention/retry, takeover, tenant suspension, and terminal closure. Release identity, queue derivation, and concurrent old/new dispatch are covered outside workflow history; add deployment rollback and drain tests to a future live deployment harness.

### 4. End-to-end operator tests

Keep Playwright small but meaningful. Seed state through supported test fixtures or APIs, not browser-only mocks.

Add live-stack cases for:

- Exact approval, suggestion/revision, old-proposal rejection, and final approval.
- A genuinely dead-lettered outbox message, reasoned requeue, disappearance from the queue, and retained history.
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

Every mutating adapter contract must cover stable idempotency keys, timeout-after-success, lookup/reconciliation, definitive versus ambiguous failure, malformed provider responses, rate limits, and tenant credential selection. Domain-specific suites add email threading/bounce/opt-out, calendar time-zone/DST/conflict behavior, payment webhook duplication/expiry, and booking concurrency.

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

1. Full tenant-table RLS/grant audit plus migration upgrade/round-trip CI.
2. The Temporal race matrix for timer/event, takeover, review, result, and Continue-As-New combinations.
3. A reusable scenario specification that runs through production kernel services and Temporal, replacing duplicate happy-path logic.
4. Live-stack Playwright coverage for review revision, dead-letter recovery, intervention, and partial scopes.
5. The first deterministic agent-evaluation corpus and shared messaging adapter contract.
6. Load, fault-injection, security, and provider-sandbox suites after communication safety and real integrations exist.

## Definition of done for a feature

A feature is complete only when its deterministic contract, persistence/authorization behavior, Temporal ordering and recovery behavior where applicable, operator visibility, failure path, and documentation are covered at the lowest useful layer. New workflow commands or snapshot fields require replay evidence. New provider side effects require shared adapter-contract evidence. New model behavior requires evaluation evidence. A happy-path browser test alone is never sufficient for a durable-agent feature.
