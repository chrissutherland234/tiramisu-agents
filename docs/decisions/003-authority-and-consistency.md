# ADR-003: Authority and consistency boundaries

- Status: Accepted
- Date: 2026-08-29

## Context

Temporal, PostgreSQL, and external providers cannot commit one atomic transaction.

## Decision

Temporal workflow state is authoritative for orchestration, waits, and command eligibility. PostgreSQL is authoritative for application configuration, received-event and audit ledgers, memory, and rebuildable projections. External domain systems are authoritative for their booking, payment, calendar, and CRM facts.

Every Signal or Update references a persisted event or command ID. Transactional inbox/outbox delivery, idempotent consumers, reconciliation, and projection watermarks recover consistency. A recorded command may still be rejected by the workflow as stale or inapplicable.

## Consequences

The system promises recoverable at-least-once delivery, not fictional distributed exactly-once semantics. UI projections may lag and must expose freshness where material.
