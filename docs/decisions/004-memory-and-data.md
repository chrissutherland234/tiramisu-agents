# ADR-004: Application-owned memory and data handling

- Status: Accepted for MVP
- Date: 2026-08-29

## Context

Long-running agents need compact, durable context. Provider-managed conversation state alone is insufficient for audit, recovery, retention, and tenant policy. Model summaries can also turn an inference into an apparent fact.

## Decision

PostgreSQL stores normalized application history, typed memory, and generated context snapshots. Do not mix local replay with SDK Sessions, OpenAI Conversations, or `previous_response_id`. Distinguish authoritative facts, customer claims, inferences, and summaries with provenance, timestamps, sensitivity, and status.

Use opaque identifiers in Temporal history where possible. Load sensitive content inside Activities, redact observability data, and make OpenAI storage/tracing policy explicit per tenant. Cross-process customer memory requires a permitted purpose.

## Consequences

The application owns compaction and context assembly. A future conversation-state strategy requires a new ADR and retry-safe migration.
