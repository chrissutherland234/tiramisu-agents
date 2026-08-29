# ADR-009: Bounded autonomy and conversational HITL

- Status: Accepted for MVP
- Date: 2026-08-29

## Context

Binary approve/reject is insufficient for real business review. Unbounded autonomous follow-ups, costs, or financial authority are unsafe.

## Decision

Enforce limits outside the prompt for turns, actions, follow-ups, timer horizons, process lifetime, tokens, cost, communication frequency, quiet hours, and consent. Provide tenant and platform circuit breakers.

HITL supports approve, reject, ask/comment, typed fact correction, cancel, and request revision. Material feedback supersedes the old proposal and triggers a bounded turn from the same logical agent. The new exact proposal requires fresh approval; there is no ambiguous “approve with changes.” Preserve review threads and proposal lineage durably.

## Consequences

Review chat semantics belong in the kernel before the polished UI. Numeric limits remain configurable within conservative platform maxima.
