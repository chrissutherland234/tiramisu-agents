# ADR-002: Proposal-only agent Activity for the MVP

- Status: Accepted for MVP
- Date: 2026-08-29

## Context

Model execution is nondeterministic and may retry. Business side effects and lifecycle invariants require stronger control than prompt instructions provide. Temporal's OpenAI integration offers finer-grained orchestration but introduces a different execution topology that should be proven independently.

## Decision

Run one bounded OpenAI Agents SDK turn in a Temporal Activity. It receives prepared context and returns a typed proposal. It cannot invoke mutating provider tools. Deterministic workflow and policy code validate the proposal, manage waits and approvals, and execute side effects through separate Activities.

Evaluate Temporal's Agents SDK integration in a non-blocking spike. Adoption requires replay, retry, session-idempotency, HITL, Continue-As-New, and worker-upgrade tests while preserving the kernel contracts.

## Consequences

The MVP favors a simple, replaceable model boundary over per-tool SDK durability. Read-only reasoning helpers remain possible inside the agent Activity.
