# ADR-008: Single-flight mailbox semantics

- Status: Accepted
- Date: 2026-08-29

## Context

Signals, operator Updates, approvals, and timers can arrive while an agent turn or Activity is in progress.

## Decision

Each process has one deterministic mailbox. Only one reasoning turn or mutating command may commit at a time. Incoming items retain their event IDs and are processed after the current boundary. Compatible items may be coalesced without losing identity. Cancellation, takeover, and safety commands have priority. Approved actions are revalidated immediately before execution.

Define and test timer/event ties, late events, review feedback during a turn, handler completion, and Continue-As-New carry-forward.

## Consequences

The workflow is easier to reason about, but deliberate batching and priority rules may add small processing latency.
