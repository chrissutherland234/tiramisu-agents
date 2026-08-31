# Temporal recovery

Tiramisu treats Temporal delivery as recoverable at-least-once messaging rather than distributed exactly-once execution. PostgreSQL owns business records, the transactional outbox owns notification intent, and one stable Temporal workflow ID owns the live mailbox for a process.

## Continue-As-New boundary

The mailbox rolls over only between completed turns. It requests Continue-As-New after 100 completed turns in a run, or earlier when Temporal reports that the current history should be continued. An Activity or automatic action-result chain is never cut in half.

The next run receives a versioned continuation snapshot containing:

- process and process-definition identity;
- buffered events, reviews, and action-resolution commands;
- all workflow-level delivery deduplication IDs;
- pending action requests and approvals;
- the active event types and absolute timer deadline;
- recent wake, turn, and execution diagnostics;
- lifetime turn and Continue-As-New counters.

The workflow ID does not change. Temporal assigns a new run ID and links the runs. Full event, decision, action, review, and process-state history remains in PostgreSQL; the continuation payload retains only the 50 most recent diagnostic records so old workflow observations do not grow without bound. Delivery deduplication IDs are currently retained for the process lifetime to keep uncertain outbox redelivery safe.

An unknown continuation schema fails the workflow explicitly and non-retryably instead of silently discarding state or looping workflow tasks. A future schema change must add a deterministic migration and a replay fixture before deployment.

## Recovery properties under test

The Temporal test environment verifies that:

- an open wait and buffered event survive a complete worker stop and a fresh worker start;
- a retried Signal remains deduplicated after a Continue-As-New boundary;
- buffered events, a pending approval, action execution state, and an absolute timer survive multiple runs;
- transient action-persistence failures retry that Activity without rerunning the model Activity;
- committed historical signal/wait and Activity-backed Continue-As-New executions replay with the current workflow code.

Run these checks locally with:

```bash
uv run pytest backend/tests/integration/test_temporal_mailbox.py backend/tests/replay
```

The replay JSON files under `backend/tests/replay/` are compatibility fixtures, not disposable generated output. Add a new fixture before intentionally changing command order or a continuation schema; do not rewrite old fixtures merely to make an incompatible workflow pass.

## Operator recovery

For a worker outage, restore workers against the same Temporal namespace and task queue with the same deployment-authorized tenant assignments. Temporal resumes outstanding workflow and Activity tasks; the PostgreSQL dispatcher resumes pending messages and reclaims stale publishing claims. Signal-With-Start and workflow-level IDs make uncertain delivery safe to retry.

After the configured bounded attempts are exhausted, the message moves to the
explicit `dead_letter` state and records its last error and dead-letter time. An
operator with `outbox:read` can inspect the queue and immutable recovery history:

```text
GET /v1/outbox/dead-letters
GET /v1/outbox/recovery-commands
```

Once the underlying incident is resolved, an operator with `outbox:requeue`
may submit a reason and stable command ID:

```text
POST /v1/outbox/dead-letters/{message_id}/requeue
```

The row is locked during recovery. Only a dead-lettered message can be
requeued, and repeating the exact command is idempotent. A successful requeue
starts a fresh bounded delivery-attempt cycle; the previous attempt count,
error, dead-letter timestamp, actor, and reason remain in an immutable recovery
command. Requeueing is safe for the supported Temporal messages because their
stable IDs and mailbox-level deduplication make delivery at least once.

Before resolving an ambiguous provider action, inspect the durable action attempt and use lookup-only reconciliation. Never manually repeat a side effect merely because a worker stopped before recording its result.

The Compose Temporal service is a persistent local development dependency, not a production topology. Production backup/restore, worker version routing and rollback, bulk dead-letter operations, alerting/observability, and restart injection at every provider boundary remain later hardening work.
