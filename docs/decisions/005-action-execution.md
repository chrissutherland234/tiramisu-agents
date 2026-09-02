# ADR-005: Action execution and reconciliation

- Status: Accepted
- Date: 2026-08-29

## Context

A provider may accept a request while the Activity times out, so an error does not prove that a side effect failed.

## Decision

Represent action requests separately from execution attempts. Every request has a stable logical key and payload hash. Use provider idempotency where available, bounded action-specific retry policies, lookup-before-retry, and explicit `UNKNOWN` and `RECONCILING` states. Compensation is explicit; irreconcilable outcomes require an operator.

An adapter may instead report a definitive `CONFLICT`: the provider conclusively rejected the requested resource or state transition. A conflict is terminal for that exact action payload, is persisted with a bounded provider-neutral code, message, details, and optionally authoritative facts, and is never automatically retried or reconciled. The owning mailbox removes it from pending work and performs one bounded result turn using the attempt as provenance. That next decision may use the supplied facts to propose a safe alternative, wait for a customer or operator, or request human review.

The platform owns this lifecycle and evidence shape, but not resource policy. Client packs and their adapters define what counts as a conflict, holds and their expiry, capacity/allocation, compensation, and communications. This keeps booking, inventory, payment, calendar, and other domains extensible without embedding their rules in the core.

Execution results are authoritative inputs to a new bounded agent turn; a decision made before provider execution cannot select the post-execution wake plan. Automatic reconciliation uses provider lookup only and never repeats the side effect. An operator resolution requires attributed evidence, is immutable and idempotent, and is delivered to the owning workflow through the transactional outbox. Follow-on proposals cite the exact action-attempt IDs that informed them.

All mutating work passes through schema validation, capability checks, deterministic policy, exact-payload approval where required, budget checks, execution, and audit.

## Consequences

Action adapters have a larger contract than a simple API wrapper. Customer-visible and financial Activities never inherit unbounded default retries. Providers without conclusive lookup may create an operator backlog, which is safer than guessing whether a side effect happened. A confirmed resource conflict cannot create a retry loop or leave a stale action pending, but it may still require an operator if no safe next step is available.
