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

## Agent-turn foundation

Versioned process definitions under `process_definitions/` are validated and compiled into deterministic decision policy before use. A PostgreSQL context loader builds a bounded snapshot from authoritative process, event, review, timer, and action-result records. The proposal-only Temporal Activity can run either a deterministic `ScriptedAgent` or the OpenAI Agents SDK adapter.

The OpenAI adapter has no tools or handoffs, permits exactly one SDK turn, and requests a strict structured output. Provider-specific action parameters cross that SDK boundary as encoded JSON and are converted back into the kernel's typed `AgentDecision`; deterministic policy then checks event lineage, allowed actions, wake events, action limits, and timer bounds.

Process definitions explicitly classify every proposed action as `allow`, `deny`, or `require_approval`; omitted actions fail closed. Retry-safe persistence records the stable action identity, immutable payload revision and hash, deterministic policy result, and—when required—an approval request bound by a database foreign key to that exact payload. A separate Temporal Activity performs this persistence after the model Activity, so retrying database work never reruns the model call.

Approval-required actions now receive a durable review thread. The review service records attributed comments, exact approve/reject decisions, and revision requests idempotently. Approval rows are locked during state changes so competing commands cannot both win; a revision request supersedes the reviewed approval and action without executing it.

Review commands are transactionally placed in the same PostgreSQL outbox as business events and delivered idempotently to the same Temporal process mailbox. A review wake carries bounded provenance into the agent context: the attributed feedback, referenced proposal parameters, rationale, revision, and payload hash. Replacement decisions must cite the review command IDs they used, and replacement proposals receive a new exact-payload approval thread.

The mailbox orchestrates event, timer, conversational-review, and action-result turns automatically. Each wake runs one bounded agent Activity followed by idempotent action and process-state persistence. Provider outcomes are loaded from PostgreSQL into a separate follow-up turn, and only that new decision may install the next wake plan. Turns remain single-flight while Signals continue buffering; automatic action-result chains are capped.

The current process projection separates provider/event-sourced authoritative facts from customer claims and records source provenance for both. Model-authored summaries and open commitments are stored separately, summaries must cite inputs from the exact bounded turn, and every applied decision creates an immutable versioned state revision containing its wake plan. Activity retries reuse the existing revision, while terminal and paused process states fail closed. This is the durable memory foundation; application-owned message history and compaction remain future work.

The Vue operator console lists tenant processes and pending reviews, then presents the selected process's durable wake plan, memory, facts and claims, commitments, and combined event/decision/action/review timeline. Operators can comment, approve the exact payload, reject it, or provide revision feedback for another bounded agent turn. The corresponding tenant-scoped APIs are intentionally available only through the unsafe local-development identity headers until production authentication and authorization are implemented.

The provider-neutral execution stage writes an action attempt before calling the provider, uses a stable payload-bound idempotency key, revalidates exact human approval immediately before dispatch, and distinguishes definitive failure from an ambiguous outcome. An unknown outcome triggers a lookup-only reconciliation Activity that cannot repeat the side effect. If the provider still cannot establish the truth, an operator may resolve it only with an immutable, attributed evidence record. That resolution is delivered transactionally through the outbox to the same Temporal mailbox and becomes authoritative context for another bounded agent turn.

The fictional client path includes stateful messaging, availability/booking, payment, and calendar adapters. The integration-free scenario driver exercises enquiry, approved correspondence, customer reply, autonomous availability lookup, approved booking, booking confirmation, approved payment request, payment completion, calendar creation, and terminal completion without Docker, network access, provider credentials, or an OpenAI key. The fictional worker uses these same action bindings.

## Development event path

The current vertical slice accepts canonical events, deduplicates them in PostgreSQL, correlates them to one process or leaves them quarantined, and transactionally schedules Temporal delivery. The dispatcher uses Signal-With-Start and workflow-level event deduplication, so retrying an uncertain delivery is safe.

There is deliberately no unauthenticated production ingestion route. For local fictional-process testing only, set:

```dotenv
TIRAMISU_ALLOW_UNSAFE_DEVELOPMENT_TENANT_HEADER=true
TIRAMISU_LOAD_FICTIONAL_EXAMPLE_PROCESSES=true
```

Then send canonical events to `POST /v1/events` with an `X-Tiramisu-Tenant-ID` header. The selected tenant must already exist. The local operator console additionally requires an arbitrary development `X-Tiramisu-Actor-ID` UUID; the Vue UI stores both IDs in browser local storage. Run delivery and workflow polling with an explicit deployment allow-list:

```bash
uv run tiramisu-worker --tenant-id 00000000-0000-0000-0000-000000000001
```

To enable model-backed orchestration for the fictional process, also set `TIRAMISU_OPENAI_MODEL` to an explicit model name. Worker startup fails closed if fictional orchestration is enabled without a model. This can make live OpenAI requests; ordinary tests continue using scripted Activities and require no API key.

The runtime role cannot enumerate tenants. Production authentication, tenant provisioning, webhook verification, and deployment-managed tenant assignments remain required before exposing ingestion externally.

## Public and private extensions

The generic platform, test kit, stub adapters, and reusable provider adapters live here. Client-specific processes, prompts, policies, proprietary adapters, evaluations, and deployment composition belong in separate private client-pack repositories. Private packs must use the same extension manifest and contract tests as the fictional public example.

## License

MIT. See [LICENSE](LICENSE).
