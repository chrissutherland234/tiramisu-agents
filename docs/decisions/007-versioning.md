# ADR-007: Independent version dimensions

- Status: Accepted
- Date: 2026-08-29

## Context

Process configuration, model behavior, event schemas, provider contracts, and Temporal workflow code evolve independently.

## Decision

Record independent versions for worker build, process definition, prompt, tool schema, model configuration, policy, event schema, memory schema, adapter contract, Tiramisu package, client pack, and extension manifest. Pin compatible versions to each process instance.

Use Temporal worker versioning and tested Continue-As-New boundaries for workflow-code changes. Migrations are explicit and audited. Live safety controls may tighten existing instances but cannot silently expand authority.

## Consequences

Compatibility metadata is a first-class domain concern. A single application version string cannot explain historical behavior.
