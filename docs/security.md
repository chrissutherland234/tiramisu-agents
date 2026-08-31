# Security operations

Tiramisu currently provides an initial managed-deployment authentication and live-safety baseline. It is suitable for controlled API clients while the broader threat model, external identity-provider integration, webhook verification, quotas, and compliance controls remain work in progress.

## Tenant credentials

The API accepts `Authorization: Bearer <token>`. A token is bound to one tenant and one attributed actor. Its database record contains only a SHA-256 digest of a randomly generated 256-bit secret, its scopes and roles, optional expiry, and revocation audit fields. The plaintext token is printed once when issued and cannot be recovered.

Available scopes are:

- `events:ingest`
- `processes:read`
- `reviews:read`
- `reviews:comment`
- `reviews:decide`

An approval can additionally require a named role. Possessing `reviews:decide` does not bypass that requirement. A bearer token is authoritative for tenant selection; development tenant headers cannot override it.

Issue a credential from a trusted administrative environment:

```bash
uv run tiramisu-admin issue-credential \
  --tenant-id 00000000-0000-0000-0000-000000000001 \
  --actor-id 00000000-0000-0000-0000-000000000002 \
  --name booking-operator \
  --scope processes:read \
  --scope reviews:read \
  --scope reviews:decide \
  --role booking_approver \
  --expires-at 2026-12-01T00:00:00+00:00
```

Rotate credentials by issuing the replacement first, changing the client, and then revoking the old credential:

```bash
uv run tiramisu-admin revoke-credential \
  --tenant-id 00000000-0000-0000-0000-000000000001 \
  --credential-id 00000000-0000-0000-0000-000000000003 \
  --actor-id 00000000-0000-0000-0000-000000000002
```

Use bearer credentials only over TLS. Keep them out of URLs, logs, source control, Temporal payloads, browser local storage, and model context. Restrict the administrative database URL and `tiramisu-admin` executable to the deployment control plane. This CLI is deliberately not exposed as an HTTP administration API.

## Deployment assignment

Client-pack services authorize a tenant through two independent controls: the
deployment's explicit UUID allow-list and the tenant's durable logical
deployment assignment. API requests, worker startup, Activities, action
execution, and outbox delivery fail closed if they disagree.

Use the attributed `tiramisu-admin assign-tenant-deployment` command shown in
[configuration.md](configuration.md); never update assignment or process release
columns directly. Assignment is serialized against event ingestion. Moving a
tenant between logical deployments is rejected until all processes are terminal
and every delivery from the old deployment is published. Active-process
migration is intentionally unsupported.

## Tenant suspension

Suspend all new autonomous work for a tenant with an attributed reason:

```bash
uv run tiramisu-admin set-tenant-status \
  --tenant-id 00000000-0000-0000-0000-000000000001 \
  --actor-id 00000000-0000-0000-0000-000000000002 \
  --status suspended \
  --reason "Incident INC-42: provider behaviour under investigation"
```

Suspension is independently checked at API authentication, event ingestion, PostgreSQL-to-Temporal dispatch, immediately before a model call, and immediately before a provider side effect. Pending outbox messages and workflows remain durable, and resume when the tenant is explicitly returned to `active`. Lookup-only reconciliation remains available because it does not repeat a side effect.

Outbox operations use separate least-privilege scopes. `outbox:read` permits
dead-letter and recovery-history inspection; `outbox:requeue` permits an
attributed requeue after bounded delivery exhaustion. A requeue never changes
the message identity or payload, only starts a fresh bounded attempt cycle, and
the previous error plus actor-supplied reason remain immutable audit evidence.

Each suspend/resume transition creates an immutable reasoned safety event. Repeating the current status is rejected so operators cannot mistake a no-op for a new transition.

A suspension cannot cancel a provider request that has already crossed the final check and begun executing. Provider idempotency, durable action attempts, lookup-only reconciliation, and the incident procedure remain necessary for that race. Resume with the same command and `--status active` only after the cause is understood.

## Current boundary

The unsafe UUID headers are accepted only when the application is explicitly in `development` and `TIRAMISU_ALLOW_UNSAFE_DEVELOPMENT_TENANT_HEADER=true`; they grant local wildcard authority and must never be enabled in a deployment. The Vue console currently uses this development path. Production human access still needs short-lived browser sessions backed by an external identity provider rather than placing deployment bearer credentials in frontend storage.
