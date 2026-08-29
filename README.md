# Tiramisu

Tiramisu is an open-source foundation for durable, long-running business agents. One logical agent follows a customer journey or case, performs bounded reasoning turns, and sleeps durably in Temporal until an approved event, timer, or human interaction wakes it.

The project is in its foundation phase. See [PLAN.md](PLAN.md) and the [architecture decisions](docs/decisions/README.md) before treating any API as stable.

## Current shape

- Python 3.13+ editable package managed with `uv`
- FastAPI backend
- Temporal workflow orchestration
- PostgreSQL application state
- OpenAI Agents SDK behind a proposal-only boundary
- Vue 3 and TypeScript operator interface
- Provider-neutral ports with deterministic stubs

## Local setup

1. Copy `.env.example` to `.env`. An OpenAI API key is needed only for model-backed evaluations or agent Activities.
2. Run `uv sync --all-groups`.
3. Run `docker compose up -d` to start PostgreSQL and the Temporal development server.
4. Run `uv run alembic upgrade head` with the migration/admin database URL.
5. Run `uv run pytest`.
6. Run `uv run tiramisu-api` for the API.
7. From `frontend`, run `npm install` and `npm run dev`.

The API uses a least-privilege `tiramisu_app` database role; Alembic uses the separate local admin role. Every application transaction must call `set_tenant_context` before accessing tenant-owned tables. The PostgreSQL integration test is opt-in locally with `TIRAMISU_RUN_DB_TESTS=1`. Temporal's mailbox test uses the official time-skipping test server. No test needs an OpenAI key.

The pure kernel and scripted-agent tests remain integration-free: they require no Docker, Temporal, PostgreSQL, network access, or provider credentials.

## Public and private extensions

The generic platform, test kit, stub adapters, and reusable provider adapters live here. Client-specific processes, prompts, policies, proprietary adapters, evaluations, and deployment composition belong in separate private client-pack repositories. Private packs must use the same extension manifest and contract tests as the fictional public example.

## License

MIT. See [LICENSE](LICENSE).
