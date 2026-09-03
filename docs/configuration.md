# Runtime configuration

Tiramisu reads backend settings from environment variables prefixed with `TIRAMISU_` and from the repository-level `.env` file. `OPENAI_API_KEY` deliberately keeps its standard name. Copy `.env.example` for local development; never commit the resulting `.env`.

Startup validation rejects malformed PostgreSQL URLs, log levels, duplicate tenant assignments, incomplete deployment identity, unsafe identity headers outside development, and the fictional client pack outside development or tests. Blank model and API-key values normalize to absent values rather than passing startup checks as empty strings.

Production API identity is data-backed rather than configured through an environment secret. A trusted deployment operator issues tenant-bound bearer credentials with `tiramisu-admin`; see [security.md](security.md). The CLI uses `TIRAMISU_MIGRATION_DATABASE_URL` because credential and tenant-status changes are control-plane operations. The API validates credentials using the restricted runtime connection.

## Service boundaries

| Setting | Used by | Meaning |
|---|---|---|
| `TIRAMISU_DATABASE_URL` | API and worker | Least-privilege application connection |
| `TIRAMISU_MIGRATION_DATABASE_URL` | Alembic | Schema-owner/admin connection |
| `TIRAMISU_TEMPORAL_TARGET` / `TIRAMISU_TEMPORAL_NAMESPACE` | Worker | Temporal frontend and environment namespace |
| `TIRAMISU_DEPLOYMENT_ID` | API, worker, and admin CLI | Stable logical deployment ID, shared across its releases |
| `TIRAMISU_DEPLOYMENT_BUILD_ID` | API and worker | Immutable artifact/build identity for this release |
| `TIRAMISU_DEPLOYMENT_TENANT_IDS` | API and worker | JSON array of deployment-authorized tenant UUIDs |
| `TIRAMISU_API_HOST` / `TIRAMISU_API_PORT` | API entry point | Bind address and port |
| `TIRAMISU_OPENAI_MODEL` / `OPENAI_API_KEY` | Model-backed worker | Explicit model and credential |
| `TIRAMISU_CLIENT_PACK_FACTORY` | API and worker | Explicit trusted `module:attribute` deployment factory |
| `VITE_API_BASE_URL` | Browser build | Public API base path, `/api` by default |
| `VITE_API_PROXY_TARGET` | Vite development server | Local backend proxy target |

When a client pack is enabled, the deployment ID, build ID, tenant list, and model ID are mandatory. Tiramisu combines them with the Tiramisu version and canonical client-pack fingerprint to derive an immutable release fingerprint and a task queue named `tiramisu.<deployment-id>.<release-fingerprint>`. Operators do not configure the queue independently.

Repeated worker `--tenant-id` arguments replace, rather than merge with, `TIRAMISU_DEPLOYMENT_TENANT_IDS`. This prevents a command-line deployment assignment from accidentally retaining broader environment scope. A worker refuses to start without at least one assignment. `TIRAMISU_WORKER_TENANT_IDS` remains a temporary compatibility alias, but new deployments should use the deployment-wide name because the API enforces the same list.

The environment allow-list is only one fence. Each tenant also has a durable logical deployment assignment. Assign it with the trusted control plane before starting services:

```bash
uv run tiramisu-admin assign-tenant-deployment \
  --tenant-id 00000000-0000-0000-0000-000000000001 \
  --deployment-id acme-bookings \
  --actor-id 00000000-0000-0000-0000-000000000002 \
  --reason "Install the Acme booking deployment"
```

The command is idempotent for the same assignment and records every real change. Moving a tenant to a different logical deployment is refused while it has a nonterminal process or an unpublished/dead-lettered delivery.

The root `.env` is also the Vite environment directory, so frontend and backend local configuration have one source. Only `VITE_` variables are exposed to browser code. Secrets must never use that prefix.

## Fictional client pack

`TIRAMISU_LOAD_FICTIONAL_EXAMPLE_PROCESSES=true` enables the bundled enquiry-to-booking project. The API and worker compile the same packaged, versioned declarations into its definition, manifest, policy IDs, strict output schema, and bindings. Composition fails before polling if Tiramisu compatibility, definition identity, integration IDs, policies, allowed actions, business metadata, or concrete adapter registrations disagree.

The API and model-backed worker require a nonblank `TIRAMISU_OPENAI_MODEL` because the model identity is part of the immutable release. Only the worker requires `OPENAI_API_KEY`. The key is passed explicitly to the OpenAI Agents SDK provider; it is not assumed to leak from dotenv parsing into the global process environment.

The unsafe development identity header switch is intentionally separate authority and is rejected outside `development`. Neither switch is a production authentication mechanism.

## Downstream client packs

`TIRAMISU_CLIENT_PACK_FACTORY=package.module:create_client_pack` selects a zero-argument factory from an installed or editable Python package. The factory must return `tiramisu_agents.extensions.ClientPack`. Both API and worker use this setting: the API installs the pack's trigger rules and definition registry, while the worker installs the same registry, strict agent output type, policies, and action-adapter bindings.

Startup validates the Tiramisu version constraint, manifest/definition identities, trigger uniqueness, integration and concrete adapter IDs, allowed-action bindings, policy IDs, and the strict output conversion boundary. Invalid composition fails before API use or worker polling. Configure either this setting or `TIRAMISU_LOAD_FICTIONAL_EXAMPLE_PROCESSES`, never both.

The factory path is deployment-controlled executable code, not tenant input or automatic plugin discovery. The current service process loads one composition for its assigned tenants. It does not yet support runtime enable/disable, persisted installation inventory, tenant-specific adapter routing within one worker, or client-defined Temporal Activities.

## Local dependencies

Docker Compose runs PostgreSQL and Temporal development dependencies. PostgreSQL data and the Temporal SQLite development store both use named volumes, so ordinary container recreation retains workflow waits and application records. The Temporal CLI image is pinned because `latest` can change the bundled server and UI incompatibly. This development server is not a production Temporal deployment.

If `POSTGRES_DB`, `POSTGRES_USER`, or `POSTGRES_PASSWORD` changes, update the two Tiramisu database URLs consistently. Alembic must continue to use the admin identity and application services the restricted runtime identity.
