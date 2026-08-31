# Local fictional demo

This guide runs Tiramisu against an existing local PostgreSQL server and Temporal
development server. Do not start this repository's Compose services when either
port is already in use.

The demo creates only a dedicated `tiramisu` database and its restricted runtime
role. It does not use, migrate, or share a client application's database.

## Prerequisites

- Python 3.13 or 3.14 and `uv`
- Node.js and npm
- PostgreSQL, with an administrative local connection
- Temporal frontend available at `localhost:7233` (or a configured alternative)

The fictional worker makes an OpenAI call for each agent turn. The API and
operator-console smoke path below do not need an OpenAI key; the full fictional
journey does.

## 1. Prepare a dedicated PostgreSQL database

Run the following as a local PostgreSQL administrator, replacing the password
and database owner with values appropriate to your machine. The runtime role
must exist *before* migrations run so the migrations can grant it the exact
table permissions it needs.

```sql
CREATE ROLE tiramisu_app LOGIN PASSWORD 'replace-with-a-local-password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE tiramisu OWNER your_local_postgres_admin;
GRANT CONNECT ON DATABASE tiramisu TO tiramisu_app;
```

Use your normal local PostgreSQL administrator as the migration connection; it
owns schema changes. The `tiramisu_app` role is only for the API and worker.

## 2. Configure the repository

Copy the example and change both URLs to the port, database owner, and runtime
password from the previous step. The sample uses a local PostgreSQL instance on
the conventional `5432` port; that is fine when it is your existing server.

```bash
cp .env.example .env
```

For the fictional local path, set these values in `.env`:

```dotenv
TIRAMISU_DATABASE_URL=postgresql+asyncpg://tiramisu_app:replace-with-a-local-password@localhost:5432/tiramisu
TIRAMISU_MIGRATION_DATABASE_URL=postgresql+asyncpg://your_local_postgres_admin@localhost:5432/tiramisu
TIRAMISU_TEMPORAL_TARGET=localhost:7233
TIRAMISU_TEMPORAL_NAMESPACE=default
TIRAMISU_ALLOW_UNSAFE_DEVELOPMENT_TENANT_HEADER=true
TIRAMISU_LOAD_FICTIONAL_EXAMPLE_PROCESSES=true
TIRAMISU_DEPLOYMENT_ID=local-fictional
TIRAMISU_DEPLOYMENT_BUILD_ID=local-demo-1
TIRAMISU_DEPLOYMENT_TENANT_IDS=["00000000-0000-0000-0000-000000000001"]
TIRAMISU_OPENAI_MODEL=your-supported-model
```

`TIRAMISU_ALLOW_UNSAFE_DEVELOPMENT_TENANT_HEADER` is strictly for this local
demo. It must remain false in every deployed environment. The API does not call
OpenAI, but the model identity is still required because it is part of the
immutable deployment release shared with the worker.

## 3. Install, migrate, and create the local tenant

```bash
uv sync --all-groups
uv run alembic upgrade head
uv run tiramisu-admin bootstrap-local
```

Migrations `20260901_12` and `20260901_13` intentionally mark historical pack and
deployment identities as unverified. Those existing processes will wait for
operator intervention instead of making model or provider calls. For the
fictional demo, ingest a new enquiry after upgrading; do not manually copy new
fingerprints or task queues onto historical rows.

The final command is repeatable and prints the identity used by the Vue console:

```json
{
  "actor_id": "00000000-0000-0000-0000-000000000002",
  "deployment_id": "local-fictional",
  "tenant_id": "00000000-0000-0000-0000-000000000001"
}
```

For a non-demo tenant, use the trusted control-plane command instead:

```bash
uv run tiramisu-admin create-tenant --slug acme-demo --name "Acme Demo"
```

Then assign the returned tenant ID to a logical deployment with an attributed
control-plane command:

```bash
uv run tiramisu-admin assign-tenant-deployment \
  --tenant-id <tenant-uuid> \
  --deployment-id acme-demo \
  --actor-id <operator-uuid> \
  --reason "Install the Acme demo deployment"
```

## 4. Start the API and operator console

In separate terminals:

```bash
uv run tiramisu-api
```

```bash
cd frontend
npm install
npm run dev
```

Open <http://127.0.0.1:5173>, enter the tenant and actor IDs printed by
`bootstrap-local`, and select **Connect locally**. A successful empty view is a
real API connection; it shows `0 processes` until an event is ingested.

## 5. Send a fictional enquiry

With the API running, submit this synthetic event. The external reference is
required because a trigger must create a stable correlation for the new process.

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/events \
  -H 'Content-Type: application/json' \
  -H 'X-Tiramisu-Tenant-ID: 00000000-0000-0000-0000-000000000001' \
  --data '{
    "event_type": "enquiry.created",
    "source": "local.demo",
    "source_event_id": "local-demo-enquiry-001",
    "occurred_at": "2026-08-31T00:00:00Z",
    "external_references": [{
      "provider": "local.demo",
      "resource_type": "enquiry",
      "external_id": "local-demo-enquiry-001"
    }],
    "facts": [{
      "key": "customer.email",
      "kind": "authoritative",
      "value": "demo@example.test"
    }, {
      "key": "customer.initial_request",
      "kind": "customer_claim",
      "value": "I would like to book next week."
    }],
    "payload": {
      "email": "demo@example.test",
      "message": "I would like to book next week."
    }
  }'
```

Refresh the console: the process appears immediately, because event ingestion
creates its durable process and queues Temporal delivery transactionally.

## 6. Run the fictional worker (requires an OpenAI key)

To execute agent turns and advance the complete fictional workflow, add a
nonblank key to `.env` (and replace the placeholder model if needed), then start
the worker:

```dotenv
OPENAI_API_KEY=your-key
```

```bash
uv run tiramisu-worker --tenant-id 00000000-0000-0000-0000-000000000001
```

The worker refuses to start without a model, key, release identity, explicit
allow-list, and matching durable tenant assignment. This avoids silently running
a journey under the wrong client-pack release.

## Smoke checks

The normal test suite includes database integration tests when
`TIRAMISU_RUN_DB_TESTS=1`. The operator-console smoke check starts the API,
connects the actual Vue application through Vite's `/api` proxy, and verifies
the local development identity against PostgreSQL:

```bash
(
  export TIRAMISU_DATABASE_URL='postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu_test'
  export TIRAMISU_MIGRATION_DATABASE_URL='postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test'
  export TIRAMISU_RUN_DB_TESTS=1
  export TIRAMISU_ALLOW_UNSAFE_DEVELOPMENT_TENANT_HEADER=false
  export TIRAMISU_LOAD_FICTIONAL_EXAMPLE_PROCESSES=false
  uv run pytest backend/tests/integration
)
cd frontend
npx playwright install chromium
npm run test:operator-smoke
```

Use the database URLs for your environment in the subshell above; do not source
`.env` as shell syntax because structured values such as JSON are not shell-safe.
The smoke command expects the database to have been migrated. It runs
`bootstrap-local` itself and is safe to repeat.
