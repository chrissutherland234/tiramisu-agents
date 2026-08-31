# Architecture and testing stocktake

Date: 31 August 2026
Reviewed commit: `2198cce`

## Overall assessment

Tiramisu now has a credible local reference implementation rather than a scaffold. It can create one durable process from an event, correlate later business objects, run bounded model turns in Temporal Activities, persist memory and lifecycle projections, enforce typed action policy and exact-payload approval, execute idempotent stub actions, reconcile ambiguous outcomes, sleep on events/timers/human review, survive worker restarts and Continue-As-New, expose operator controls, and recover dead-lettered delivery.

The architectural center is sound: Temporal owns orchestration, PostgreSQL owns business/audit state, and the model is proposal-only. The recent hardening work closed the most dangerous lifecycle and delivery races found in the prior review.

It is not yet a production multi-client platform. The most important remaining work is not another business primitive. It is enforcing pinned configuration over months-long process lifetimes, deciding how different client packs map to deployments and tenants, completing the communication/data safety envelope, and making the test strategy exercise the real kernel rather than parallel demonstrations.

## Findings

### P0 — Pinned client-pack compatibility is recorded but not enforced

`ProcessInstance.extension_manifest_hash` is written when a process starts, but the agent Activity selects the current registry definition using only process type and definition version. It does not compare the stored manifest hash or definition fingerprint with the loaded worker composition. A pack can therefore change a prompt, policy, output conversion, or adapter while retaining the same process/version identifiers, and an existing months-old process will silently execute the new behavior.

This violates the plan's immutable-version invariant and ADR-007. Before client-pack evolution is relied upon, every turn and action must fail closed unless the process's pinned composition is available and compatible. Definition publication must make identities immutable; changes require a new version or an explicit, audited active-instance migration.

### P0 — Multi-client deployment topology required a decision

The database and credentials are tenant-scoped, but `TIRAMISU_CLIENT_PACK_FACTORY`, process registry, strict output type, and action bindings are process-wide. Every tenant assigned to one API/worker deployment therefore receives the same composition and action-type namespace. This cannot currently host two clients with different processes or provider credentials in one service deployment.

The recommended near-term answer is a deployment unit per client pack (or per group of tenants sharing an identical pack), with its own task queue and immutable build. A thin control plane or ingress router may map tenants to deployments later. Building a dynamic tenant-aware adapter/model registry now would add substantial complexity to every safety boundary.

Decision update: this topology was accepted after the review and recorded in ADR-011. Deployment identity, routing inventory, and rollout controls still need implementation.

### P1 — The Python extension boundary is trusted code, not a sandbox

An installed pack factory and its adapters/output conversion are arbitrary Python. The `ClientPack` contract validates declared identities and ensures the stock worker wires registered actions through the gateway, but it cannot prevent import-time I/O, hidden side effects, data access, or intentionally bypassing platform services.

The plan and ADR previously described extensions as unable to bypass core safety too absolutely. The accurate security model is: client packs are trusted, reviewed deployment artifacts; the supported registration path is fail-closed; CI contracts detect accidental drift; isolation from malicious extensions requires a separate process/service boundary, not Python typing.

### P1 — Draft definitions and deployment compatibility need a publication fence

`ClientPack.trigger_rules()` currently creates bootstrap rules for all definitions regardless of `draft`, `published`, or `retired` status. That is acceptable for the explicitly fictional development pack, but unsafe as the generic downstream path. Production ingress must start only a published immutable definition. Draft execution should require an explicit simulation/test mode and must not share production triggers.

### P1 — The safety and context envelope is incomplete

Follow-up count/interval and tenant suspension are enforced, but token/cost/process-lifetime budgets, quiet hours, opt-out, auto-responder prevention, rate limits, and capability circuit breakers are absent. Canonical event payloads, fact collections/values, action parameter JSON, and total rendered model context also lack useful byte/token ceilings.

This blocks real email or other untrusted event sources. Large or adversarial inputs can currently create storage, model-cost, and prompt-safety problems before any business policy is evaluated.

### P1 — The pure journey test follows a parallel simplified lifecycle

`FictionalJourneyDriver` evaluates policy and calls adapters directly using its own in-memory action records. It does not exercise the production action gateway, approval persistence, process projection, context loader, or orchestration. The real PostgreSQL/Temporal journey covers the production path, but the advertised reusable integration-free kernel scenario does not yet exist.

The scenario should become data plus drivers that can target the same production services at multiple layers. Otherwise the pure journey can remain green while the real kernel regresses.

### P2 — Test depth is concentrated in a few large scenarios

Temporal and database coverage is unusually good for this stage, but only two histories are replayed, the direct RLS audit samples one table, Vue has two component tests, and Playwright has one live-stack case. The dead-letter UI's real requeue path, conversational proposal revision, stale proposal rejection, intervention retry, partial credential scopes, migration upgrades from prior releases, and broad failure/race combinations are not end-to-end gates.

There is no agent behavior evaluation harness, shared adapter contract suite, provider sandbox suite, property/state-machine testing, load testing, or automated fault injection beyond focused fakes.

### P2 — Operational and public-repository controls remain foundational

Health is liveness metadata rather than dependency readiness. Structured correlation logs, metrics, traces, backlog/stuck-work alerts, data redaction, payload encryption, retention/deletion, secret scanning, dependency review, SBOMs, and backup/restore evidence remain outstanding. These are correctly planned as production work, but should precede claims of a managed multi-tenant service.

## Test coverage snapshot

At the reviewed commit, all 106 backend tests passed with PostgreSQL enabled, including 11 Temporal workflow cases and 2 replay fixtures. Vue had 2 passing component tests and 1 passing Playwright live-stack smoke. CI also checked lint, formatting, strict typing, Alembic drift, package builds, Compose startup, PostgreSQL runtime-role access, and Temporal health.

The strongest areas are initiating-event races, outbox ownership/recovery, action authorization and execution fencing, tenant Activity authorization, process intervention, restart/Continue-As-New, and the complete scripted journey. The weakest are model behavior, configuration evolution, multi-pack topology, communication abuse controls, reusable cross-layer scenarios, provider contracts, UI failure paths, migration evolution, security automation, and load/resilience.

The actionable gap plan is maintained in [`docs/testing.md`](../testing.md).

## Recommended next sequence

1. Enforce pinned manifest/definition compatibility and published-only production triggers, with regression and replay tests.
2. Implement ADR-011's deployment identity, task-queue boundary, tenant assignment, upgrade, rollback, and active-instance migration rules.
3. Add input/context ceilings plus communication safety: opt-out, quiet hours, auto-responder/loop detection, rate and lifetime/token/cost budgets.
4. Implement operator quarantine resolution and replay; it is the largest missing recovery loop in the current event path.
5. Refactor the scenario test kit to drive production kernel services, then fill the Temporal race, RLS, migration, and browser matrices.
6. Add the first real-model evaluation corpus and a shared messaging adapter contract.
7. Only then add a real email provider. A GitHub-issue triage/Codex handoff pack is a useful later second-pack test, but should validate the chosen deployment boundary rather than define it accidentally.

## What should not be done next

- Do not add several real providers before communication safety, adapter contracts, and reconciliation semantics are complete.
- Do not build dynamic multi-pack discovery or per-tenant Python imports inside workers or workflows.
- Do not treat the current editable example—which delegates to the bundled fictional pack—as proof that an independently authored pack is portable.
- Do not use raw test count or the happy reference journey as a production-readiness claim.
- Do not promise that trusted in-process Python extensions are technically unable to bypass the host application.
