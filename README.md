# Tiramisu

Tiramisu is an open-source foundation for durable, long-running business agents. One logical agent follows a customer journey or case, performs bounded reasoning turns, and sleeps durably in Temporal until an approved event, timer, or human interaction wakes it.

The project is in its foundation phase. See [PLAN.md](PLAN.md), the [testing strategy](docs/testing.md), the [platform safety limits](docs/safety-limits.md), the [local fictional demo guide](docs/local-demo.md), the [runtime configuration guide](docs/configuration.md), the [client-pack composition guide](docs/client-packs.md), the [security operations guide](docs/security.md), the [Temporal recovery guide](docs/temporal-recovery.md), and the [architecture decisions](docs/decisions/README.md) before treating any API as stable.

## Current shape

- Python 3.13+ editable package managed with `uv`
- FastAPI backend
- Temporal workflow orchestration
- PostgreSQL application state
- OpenAI Agents SDK behind a proposal-only boundary
- Vue 3 and TypeScript operator interface
- Provider-neutral ports with deterministic stubs
- Opinionated Python client projects compiled into safe runtime packs

## Local setup

1. Copy `.env.example` to `.env`. An OpenAI API key is needed only for model-backed evaluations or agent Activities.
2. Run `uv sync --all-groups`.
3. Run `docker compose up -d --wait` to start PostgreSQL and the persistent Temporal development server.
4. Run `uv run alembic upgrade head` with the migration/admin database URL.
5. Run `uv run pytest`.
6. Run `uv run tiramisu-api` for the API.
7. From `frontend`, run `npm install` and `npm run dev`.

If PostgreSQL or Temporal already run locally, point `.env` at those dedicated
services and skip Docker Compose. The [local fictional demo guide](docs/local-demo.md)
includes role setup, tenant bootstrap, the operator-console connection, and a
sample enquiry event.

The API uses a least-privilege `tiramisu_app` database role; Alembic uses the separate local admin role. Every application transaction must call `set_tenant_context` before accessing tenant-owned tables. The PostgreSQL integration test is opt-in locally with `TIRAMISU_RUN_DB_TESTS=1`. Temporal's mailbox test uses the official time-skipping test server; an offline or predownloaded binary can be selected with `TEMPORAL_TEST_SERVER_PATH`. No test needs an OpenAI key. The bundled fictional project is ordinary importable Python, so API and worker composition does not depend on the launch directory.

The pure kernel and scripted-agent tests remain integration-free: they require no Docker, Temporal, PostgreSQL, network access, or provider credentials.

## Agent-turn foundation

Versioned process definitions under `process_definitions/` are validated and compiled into deterministic decision policy before use. A PostgreSQL context loader builds a bounded snapshot from authoritative process, event, review, timer, and action-result records. The proposal-only Temporal Activity can run either a deterministic `ScriptedAgent` or the OpenAI Agents SDK adapter.

Hard platform byte and count ceilings now reject oversized canonical events before persistence, bound every source included in a turn, preflight the prospective fact projection and complete context before model I/O, and check the final rendered prompt before provider I/O. Action parameters and persistent memory are checked again at their persistence boundaries. Unsafe turn construction fails closed into durable operator intervention; model-proposed excess receives the same bounded semantic-repair opportunity as other deterministic policy errors. Exact limits and remaining raw-transport/token-budget work are documented in the [platform safety limits](docs/safety-limits.md).

Customer-contact actions are explicitly classified by each client journey. Deterministic policy
enforces process-local opt-out, automated-response suppression until a genuine human reply, local
quiet hours, rolling and process-lifetime message budgets, follow-up spacing, and maximum process
lifetime. Accepted action requests form the durable reservation ledger, so limits survive retries,
worker restarts, and Continue-As-New. The action executor repeats the check at the provider boundary;
human approval does not bypass it. The operator process view explains every current block and its
next eligible time where one exists.

The OpenAI adapter has no tools or handoffs, permits exactly one SDK invocation per proposal, and requests a strict structured output. Provider-specific action parameters cross that SDK boundary as encoded JSON and are converted back into the kernel's typed `AgentDecision`; deterministic policy then checks event lineage, allowed actions, wake events, action limits, and timer bounds. If policy rejects a typed proposal, the Activity can request at most two complete replacements using the exact validator error and rejected proposal while retaining the same loaded trusted snapshot. Only a validated proposal leaves the Activity; exhaustion follows the existing operator-intervention path.

Process definitions explicitly classify every proposed action as `allow`, `deny`, or `require_approval`; omitted actions fail closed. Retry-safe persistence records the stable action identity, immutable payload revision and hash, deterministic policy result, and—when required—an approval request bound by a database foreign key to that exact payload. A separate Temporal Activity performs this persistence after the model Activity, so retrying database work never reruns the model call.

Approval-required actions now receive a durable review thread. The review service records attributed comments, exact approve/reject decisions, and revision requests idempotently. Approval rows are locked during state changes so competing commands cannot both win; a revision request supersedes the reviewed approval and action without executing it.

Review commands are transactionally placed in the same PostgreSQL outbox as business events and delivered idempotently to the same Temporal process mailbox. A review wake carries bounded provenance into the agent context: the attributed feedback, referenced proposal parameters, rationale, revision, and payload hash. Replacement decisions must cite the review command IDs they used, and replacement proposals receive a new exact-payload approval thread.

The mailbox orchestrates event, timer, conversational-review, and action-result turns automatically. Each wake runs one bounded agent Activity followed by idempotent action and process-state persistence. Provider outcomes are loaded from PostgreSQL into a separate follow-up turn, and only that new decision may install the next wake plan. Turns remain single-flight while Signals continue buffering; automatic action-result chains are capped.

Rejected turns and exhausted automatic chains enter a durable intervention state instead of disappearing inside workflow history. Tenant-scoped operators can issue attributed, idempotent retry, wake, takeover, and resume controls through the API or Vue console. Wake and Resume persist a reserved manual-wake event that bypasses the current business-event filter and causes one bounded reevaluation turn; its reason is guidance and cannot mutate authoritative facts. Retry preserves the failed turn's bounded source lineage.

Mailbox executions Continue-As-New at a safe boundary after a bounded number of completed turns or when Temporal recommends rollover. A versioned snapshot preserves buffered and deduplicated events, review and reconciliation commands, pending approvals, the active event/timer wake plan, and process/version identity under the same workflow ID. CI replays committed histories—including an Activity-backed rollover, manual reevaluation, and takeover during a model turn—and injects persistence retries to verify they do not rerun the model Activity. Time-skipping race tests pin the ordering of events, timers, reviews, action results, lifecycle controls, and rollover buffers.

The current process projection separates provider/event-sourced authoritative facts from customer claims and records source provenance for both. Model-authored summaries and open commitments are stored separately, summaries must cite inputs from the exact bounded turn, and every applied decision creates an immutable versioned state revision containing its wake plan. Activity retries reuse the existing revision, while terminal and paused process states fail closed. This is the durable memory foundation; application-owned message history and compaction remain future work.

The Vue operator console lists tenant processes and pending reviews, then presents the selected process's durable wake plan, memory, facts and claims, commitments, and combined event/decision/action/review timeline. Operators can comment, approve the exact payload, reject it, provide revision feedback for another bounded agent turn, inspect dead-lettered deliveries, open their related process, and requeue them with a required audit reason. The API has an initial tenant-bound bearer credential baseline with endpoint scopes, approval roles, expiry, revocation, and tenant suspension. The current Vue identity form remains development-only; a production browser session and external identity-provider integration are still required.

The provider-neutral execution stage writes an action attempt before calling the provider, uses a stable payload-bound idempotency key, revalidates exact human approval immediately before dispatch, and distinguishes definitive failure, a definitive resource/state conflict, and an ambiguous outcome. A conflict is persisted with bounded structured details and authoritative facts, is recoverable through lookup after a platform crash without repeating the provider operation, and drives one bounded follow-up turn. The deterministic policy and persistence gateway reject an unchanged re-proposal from that result turn, while allowing a genuinely different proposal or a later fact-driven turn. An unknown outcome triggers a lookup-only reconciliation Activity that cannot repeat the side effect. If the provider still cannot establish the truth, an operator may resolve it only with an immutable, attributed evidence record. That resolution is delivered transactionally through the outbox to the same Temporal mailbox and becomes authoritative context for another bounded agent turn.

Temporal outbox delivery uses claim tokens, exponential retries, and an explicit dead-letter state after bounded exhaustion. Credentials with `outbox:read` can inspect dead letters and recovery history; `outbox:requeue` authorizes an attributed, idempotent requeue. Requeue starts a fresh bounded attempt cycle while preserving the prior attempt count, error, timestamp, reason, and actor in immutable recovery history.

The fictional client path includes stateful messaging, availability/booking, payment, and calendar adapters. Its compiled executable scenario exercises enquiry, approved correspondence, customer reply, autonomous availability lookup, approved booking (which the fictional adapter immediately confirms), approved payment request, payment completion, calendar creation, and terminal completion without Docker, network access, provider credentials, or an OpenAI key. `tiramisu simulate tiramisu_agents.builtin:create_fictional_project --scenario happy_path` runs it through generated strict decision validation, production policies, explicitly safe adapters, and process transitions shared with persistence. `PostgresTemporalScenarioDriver` runs that identical compiled scenario through real ingestion, outbox delivery, mailbox and Activity orchestration, exact-payload review approval, durable projections, safe adapters, and fresh worker compositions at business boundaries. A separate timer scenario proves multi-hour waits through Temporal time skipping, and failures identify the exact authored scenario step. Manual reevaluation and authority behavior remain covered by their focused workflow and persistence tests.

## Development event path

The current vertical slice accepts canonical events, deduplicates them in PostgreSQL, correlates them to one process or leaves them quarantined, and transactionally schedules Temporal delivery. The dispatcher uses Signal-With-Start and workflow-level event deduplication, so retrying an uncertain delivery is safe.

There is no unauthenticated production ingestion route. Production requests require a tenant-bound bearer credential with the relevant scope. Credential lifecycle and emergency suspension are described in [docs/security.md](docs/security.md).

For local fictional-process testing only, set:

```dotenv
TIRAMISU_ALLOW_UNSAFE_DEVELOPMENT_TENANT_HEADER=true
TIRAMISU_LOAD_FICTIONAL_EXAMPLE_PROCESSES=true
```

Then send canonical events to `POST /v1/events` with an `X-Tiramisu-Tenant-ID` header. The selected tenant must already exist. The local operator console additionally requires an arbitrary development `X-Tiramisu-Actor-ID` UUID; the Vue UI stores both IDs in browser local storage. Run delivery and workflow polling with an explicit deployment allow-list, either as repeated CLI options:

```bash
uv run tiramisu-worker --tenant-id 00000000-0000-0000-0000-000000000001
```

or as a JSON environment value:

```dotenv
TIRAMISU_DEPLOYMENT_TENANT_IDS=["00000000-0000-0000-0000-000000000001"]
```

Client-pack API and worker processes also require a stable `TIRAMISU_DEPLOYMENT_ID`, an immutable `TIRAMISU_DEPLOYMENT_BUILD_ID`, and a durable matching tenant assignment created by `tiramisu-admin assign-tenant-deployment`. Tiramisu derives the Temporal queue from that release identity; it is not configured separately. See the [configuration guide](docs/configuration.md) for the local command and rollout rules.

To enable model-backed orchestration for the fictional process, set `TIRAMISU_OPENAI_MODEL` to an explicit model name for both API and worker and `OPENAI_API_KEY` to a nonblank key for the worker. Worker startup fails closed before connecting if the key or deployment configuration is absent. This can make live OpenAI requests; ordinary tests continue using scripted Activities and require no API key.

The runtime role cannot enumerate tenants. Signed provider-webhook verification, managed tenant provisioning, production browser identity, and rate limits remain required before exposing ingestion externally.

## Public and private extensions

The generic platform, project framework, test kit, stub adapters, and reusable provider adapters live here. Client-specific processes, prompts, policies, proprietary adapters, evaluations, and deployment composition belong in separate private client-pack repositories. The normal authoring path uses `Project`, `Journey`, `Route`, `Capability`, `Fact`, and `Scenario`; Tiramisu generates the manifest, process definition, bindings, policy IDs, and strict OpenAI schema. See the [client-project guide](docs/client-packs.md), the [bundled fictional project](backend/src/tiramisu_agents/builtin/fictional.py), and the independently packaged [support example](examples/support_client_pack/README.md).

Start and inspect a conventional package with:

```bash
uv run tiramisu startproject acme_service ../acme-service
uv run tiramisu check tiramisu_agents.builtin:create_fictional_project
uv run tiramisu describe tiramisu_agents.builtin:create_fictional_project
uv run tiramisu simulate tiramisu_agents.builtin:create_fictional_project --scenario happy_path
```

Compiled private packs return the stable `tiramisu_agents.extensions.ClientPack` from an explicit zero-argument startup factory. Direct construction of that low-level contract remains available for advanced cases.

Install the core and a downstream pack as editable packages, then configure the exact same trusted factory path in the API and worker environment:

```bash
uv pip install -e . -e /path/to/client-pack
export TIRAMISU_CLIENT_PACK_FACTORY=client_package:create_client_pack
```

The factory is imported and validated before API traffic or Temporal worker polling. It is never discovered or imported from workflow code. A pack is trusted executable Python, not a sandbox. Under [ADR-011](docs/decisions/011-client-pack-deployment-topology.md), each pack has its own logical deployment and immutable release queues; tenants may share it only when they intentionally share the exact pack and release lifecycle. New processes pin the release, queue, complete pack, and definition fingerprints. Old and new workers can coexist during drain, while incompatible workers stop before model or provider I/O. Persisted installation inventory, active-instance migration, and custom Activity registration remain future work.

## License

MIT. See [LICENSE](LICENSE).
