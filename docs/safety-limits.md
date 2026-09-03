# Platform safety limits

Last reviewed: 2026-09-03

Tiramisu applies deterministic hard ceilings to untrusted semantic data and every assembled
agent turn. These are platform safety maxima, not prompt instructions. A future tenant or process
policy may choose lower values, but it must not raise them without a reviewed platform release.

Sizes are measured as UTF-8 bytes. JSON sizes use compact, key-sorted, UTF-8 JSON with non-finite
numbers rejected. This makes a boundary reproducible across API validation, policy validation,
persistence, and tests.

## Initial envelope

| Boundary | Hard maximum |
| --- | ---: |
| Event payload | 100 top-level fields and 64 KiB |
| Parsed event request | 96 KiB |
| Complete canonical event | 128 KiB |
| External references per event | 20 |
| Facts per event | 50 |
| One fact value | 16 KiB |
| Events per agent turn | 50 |
| Review commands per agent turn | 20 |
| Action results per agent turn | 20 |
| Timers per agent turn | 50 |
| One action's parameters | 100 top-level fields and 32 KiB |
| One action conflict | 50 authoritative facts, 4 KiB message, and 128 KiB complete outcome |
| One review message | 16 KiB |
| Operator wake/control guidance | 16 KiB |
| Open commitments | 50 items, 2 KiB each, and 32 KiB combined |
| Memory summary | 16 KiB |
| Process fact projection | 500 facts/claims and 128 KiB including provenance |
| Complete assembled agent context | 256 KiB |
| Complete rendered model input | 512 KiB |

The parsed-request limit is intentionally above the payload limit to leave room for references,
facts, and event metadata. The canonical-event limit leaves additional room for server-owned
tenant and receipt metadata. The rendered-input limit leaves room for instructions, controlled
correction feedback, and serialization around the already-bounded context.

## Failure behavior

- An oversized API event is rejected with `422` before an inbox, process, correlation, or outbox
  row is created.
- Review and operator guidance are rejected before their control records or manual-wake event are
  persisted.
- The PostgreSQL context loader checks source counts, existing persistent memory, action/review
  parameters, the prospective fact projection, and the complete turn snapshot before model I/O.
- An unsafe context or rendered prompt becomes a non-retryable `AgentContextLimitExceeded`
  Activity failure. The workflow retains the turn lineage and enters its existing durable,
  operator-visible intervention state.
- Oversized model-proposed action parameters or memory receive the exact deterministic validation
  error within the bounded two-attempt semantic correction loop. Exhaustion enters intervention.
- The action gateway and process-state projector repeat hard checks immediately before persistence
  as defense in depth.
- Provider-declared action conflicts are rejected at contract construction if their fact count,
  encoded message, or complete structured outcome exceeds the platform envelope.

## Deliberate remaining work

These limits do not yet provide an ASGI/web-server raw request-body cap, attachment streaming
limits, general successful provider-response limits, model token/cost budgets, process-lifetime
budgets, tenant-specific lower ceilings, or per-tenant throughput/back-pressure quotas. Raw transport limits are required
before public production ingress. Token, cost, rate, and lifetime budgets belong to the next
autonomy-budget milestone and must be durable across Continue-As-New.
