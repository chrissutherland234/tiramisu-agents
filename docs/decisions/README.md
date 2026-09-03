# Architecture decision records

ADRs record durable architectural choices separately from the evolving project plan.

| ADR | Decision | Status |
| --- | --- | --- |
| [001](001-journey-aggregate.md) | Journey aggregate and external correlation | Accepted |
| [002](002-agent-execution-topology.md) | Proposal-only agent Activity | Accepted for MVP |
| [003](003-authority-and-consistency.md) | Temporal/PostgreSQL/provider authority | Accepted |
| [004](004-memory-and-data.md) | Application-owned memory and data handling | Accepted for MVP |
| [005](005-action-execution.md) | Action idempotency and reconciliation | Accepted |
| [006](006-tenancy-and-encryption.md) | Shared-schema tenancy and encryption | Accepted for MVP |
| [007](007-versioning.md) | Independent version dimensions | Accepted |
| [008](008-mailbox-ordering.md) | Single-flight mailbox semantics | Accepted |
| [009](009-autonomy-and-hitl.md) | Bounded autonomy and conversational HITL | Accepted for MVP |
| [010](010-open-source-extensions.md) | Public core and private client packs | Accepted |
| [011](011-client-pack-deployment-topology.md) | One deployment and task queue per client pack | Accepted |
| [012](012-opinionated-project-authoring.md) | Opinionated client-project authoring | Accepted |

Statuses apply to architectural direction. Numeric limits, provider choices, and operational parameters remain configuration decisions.
