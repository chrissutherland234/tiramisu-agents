# ADR-009: Bounded autonomy and conversational HITL

- Status: Accepted for MVP
- Date: 2026-08-29

## Context

Binary approve/reject is insufficient for real business review. Unbounded autonomous follow-ups, costs, or financial authority are unsafe.

## Decision

Enforce limits outside the prompt for turns, actions, follow-ups, timer horizons, process lifetime, tokens, cost, communication frequency, quiet hours, and consent. Provide tenant and platform circuit breakers.

HITL supports approve, reject, ask/comment, typed fact correction, cancel, and request revision. Material feedback supersedes the old proposal and triggers a bounded turn from the same logical agent. The new exact proposal requires fresh approval; there is no ambiguous “approve with changes.” Preserve review threads and proposal lineage durably.

For the first communication envelope, a client journey explicitly classifies outbound action types
and canonical inbound event types as genuine human replies, opt-outs, or automated responses. Those
roles are disjoint and are supplied by trusted integration adapters, not inferred from arbitrary
message text by the model.

The action ledger is the durable reservation ledger. Accepted and uncertain outbound proposals
consume process and rolling-window capacity before approval or execution; terminal proposals known
not to have produced a side effect release it. A genuine reply resets only the follow-up count and
spacing, not total or rolling usage. An opt-out is permanent for that process. An automated response
blocks further contact until a later genuine reply. Quiet intervals are start-inclusive and
end-exclusive in one configured IANA timezone. The exact process-lifetime boundary is also closed.

Evaluate communication safety when reserving a proposal and again at the provider boundary. Human
approval cannot override safety. Matched event ingestion and the provider boundary serialize on the
same process row, making the committed order of a last-second opt-out and a send explicit. The fast
scenario runner and PostgreSQL implementation use one pure evaluator; PostgreSQL only projects its
inputs from durable records.

## Consequences

Review chat semantics belong in the kernel before the polished UI. Numeric limits remain configurable within conservative platform maxima. Process-local opt-out is not a substitute for a cross-process customer consent registry, and a static journey timezone is not yet recipient-specific; both remain production messaging gates.
