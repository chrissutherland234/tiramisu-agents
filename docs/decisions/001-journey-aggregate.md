# ADR-001: Journey aggregate and external correlation

- Status: Accepted
- Date: 2026-08-29

## Context

An enquiry may later acquire booking, payment, calendar, CRM, and message-thread identifiers. None is a safe permanent identity for the complete relationship.

## Decision

Create one stable platform process ID per customer journey, case, or business intent. Use it as the logical Temporal workflow identity and agent identity. Maintain tenant-scoped external correlations from provider/resource identifiers to the process. Route unmatched or ambiguous events to quarantine; never guess. Merge, split, reopen, and late-event behavior is process policy.

## Consequences

Processes can begin before a booking exists and retain identity as provider resources change. Event ingress requires a durable correlation registry and operator resolution path.
