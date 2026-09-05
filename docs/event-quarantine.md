# Event quarantine and replay

Unmatched or ambiguous events remain in the durable inbox without waking a guessed process.
An initiating event with conflicting references is also quarantined; it cannot create another
process just because it is a configured trigger. Events rejected because their explicit process
ID is unavailable can be resolved through the same queue.

## Operator flow

Open **Event quarantine** from the dashboard or process console (`/quarantine`). The queue shows
source identities, receipt times, and the recorded correlation reason. Select an event to inspect
its original content, current reference owners, and matching processes.

Choose the destination process and provide a reason. You can select references from the event to
route future events to that process. Only unassigned references or references already belonging
to the destination can be selected. Existing ownership is never reassigned. For an ambiguous
event, resolve the event to the verified destination and leave references owned by other processes
unchecked. Leaving every reference unchecked resolves only this event.

**Resolve and replay** records the operator's decision and schedules the original event through
the normal outbox. It does not fabricate a new event, change its content or timestamps, create a
new process, or bypass the workflow's wake, approval, lifecycle, and execution rules. A terminal
process with the supported `record_only` late-event policy retains the event without a delivery.
A paused process retains its normal mailbox behavior.

Resolution history retains the actor, reason, previous correlation status/reason, destination,
selected references, and whether delivery was scheduled. The original source payload remains
available after resolution. A scheduled delivery is not proof of completed processing: inspect
delivery operations if it exhausts its normal retry cycle. Use dead-letter recovery to retry a
failed delivery; resolving an already-correlated event again is rejected.

The Vue console uses the existing local development identity headers. Production API clients use
tenant-bound bearer credentials; browser session authentication remains a separate platform task.

## API

- `GET /v1/quarantine?state=unresolved&limit=25&offset=0` lists pending/rejected events.
- `GET /v1/quarantine?state=resolved&limit=25&offset=0` lists audited resolutions.
- `GET /v1/quarantine/{event_id}` returns the original event, current reference ownership,
  matching processes, and any resolution record.
- `POST /v1/quarantine/{event_id}/resolve` resolves and schedules the original event atomically.

Inspection requires `quarantine:read`; resolution requires `quarantine:resolve`. These are separate
from event ingestion and process control. Tenant IDs and actor IDs come from authentication, never
from the resolution body. Queue responses include `can_resolve` for read-only presentation.
Pagination returns `items`, `total`, `limit`, and `offset`; refresh a changing queue to see new items.

Example resolution body:

```json
{
  "command_id": "11111111-1111-4111-8111-111111111111",
  "process_instance_id": "22222222-2222-4222-8222-222222222222",
  "reason": "Verified the email thread against the customer's enquiry.",
  "bind_references": [
    {"provider": "mail", "resource_type": "thread", "external_id": "thread-123"}
  ]
}
```

Generate the command UUID once and retain the exact body when retrying a lost response. Repeating
that command returns the original audit record, including after delivery. Reusing its ID with a
different event, destination, actor, reason, or selected reference set returns `409`. A different
command attempting to resolve an already-correlated event also returns `409`. Missing or
cross-tenant events/destinations return `404`; missing scopes and suspended or unauthorized tenants
are rejected. Invalid request shapes return `422`.

## Durability and concurrency

The inbox association, selected reference bindings, immutable resolution audit, and delivery
outbox insert share one PostgreSQL transaction. Failure rolls all of them back. Resolution shares
source and reference identity locks with ordinary ingestion, locks the destination process to
respect concurrent lifecycle changes, and holds the tenant deployment assignment lock used by
ingress. A destination in another logical deployment is rejected. Existing release pins are
preserved, and the normal dispatcher routes to the process's pinned release queue.

The original event ID is the delivery deduplication identity. Duplicate webhook receipts return
the original inbox result. Concurrent operator commands cannot resolve the same event twice.
Temporal mailbox deduplication handles delivery retries, including a response lost after a signal
was accepted. The runtime database role has only SELECT/INSERT on the resolution audit table;
forced row-level security and composite tenant foreign keys protect the audit and destination.

Migration `20260905_18` adds `event_resolution_commands`. Apply it before deploying the new API.
Downgrading removes resolution audit records; production rollback requires retaining an audit
backup or keeping this additive table while rolling back application code.

Bulk resolution, automatic replay of an entire backlog, correlation reassignment, process
merge/split/reopen, and payload editing are separate operations. A newly bound reference routes
future arrivals automatically; older quarantined events remain available for explicit review.
