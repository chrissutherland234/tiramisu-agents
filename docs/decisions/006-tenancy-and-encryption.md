# ADR-006: Shared-schema tenancy and payload protection

- Status: Accepted for MVP
- Date: 2026-08-29

## Context

Schema-per-client complicates migrations and operations, while a shared schema makes tenant-context failures consequential.

## Decision

Use one application schema with mandatory tenant IDs, tenant-scoped constraints, PostgreSQL row-level security, `FORCE ROW LEVEL SECURITY`, non-owner runtime roles, transaction-local tenant context, and pool-reset checks. Use separate migration, API, worker, and restricted support roles.

Use one Temporal namespace per environment initially. Keep PII out of workflow IDs and search attributes. Provide Payload Codec and failure-converter hooks for application-layer encryption outside local development.

## Consequences

Cross-tenant negative tests are release gates. Dedicated databases, namespaces, or deployments remain an upgrade path for contractual or regulatory isolation.
