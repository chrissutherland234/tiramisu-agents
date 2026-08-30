# ADR-005: Action execution and reconciliation

- Status: Accepted
- Date: 2026-08-29

## Context

A provider may accept a request while the Activity times out, so an error does not prove that a side effect failed.

## Decision

Represent action requests separately from execution attempts. Every request has a stable logical key and payload hash. Use provider idempotency where available, bounded action-specific retry policies, lookup-before-retry, and explicit `UNKNOWN` and `RECONCILING` states. Compensation is explicit; irreconcilable outcomes require an operator.

Execution results are authoritative inputs to a new bounded agent turn; a decision made before provider execution cannot select the post-execution wake plan. Automatic reconciliation uses provider lookup only and never repeats the side effect. An operator resolution requires attributed evidence, is immutable and idempotent, and is delivered to the owning workflow through the transactional outbox. Follow-on proposals cite the exact action-attempt IDs that informed them.

All mutating work passes through schema validation, capability checks, deterministic policy, exact-payload approval where required, budget checks, execution, and audit.

## Consequences

Action adapters have a larger contract than a simple API wrapper. Customer-visible and financial Activities never inherit unbounded default retries. Providers without conclusive lookup may create an operator backlog, which is safer than guessing whether a side effect happened.
