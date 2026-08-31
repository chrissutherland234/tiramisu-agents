# ADR-011: Client-pack deployment topology

- Status: Accepted
- Date: 2026-08-31
- Implementation note: deployment-release boundary completed 2026-09-01

## Context

Tiramisu is multi-tenant at the data and authorization layers, but a running API/worker composition currently has one process registry, strict agent output type, action-binding namespace, model configuration, and client-pack factory. Different clients will commonly require different processes, provider adapters, credentials, and release schedules.

Selecting arbitrary Python packs dynamically by tenant inside one worker would spread tenant-aware routing across model execution, actions, credentials, compatibility checks, Temporal task queues, upgrades, and rollback. It would also increase the blast radius of pack defects and make long-running version guarantees harder to reason about.

## Decision

One client pack is one independently deployable Tiramisu service unit consisting of its API and worker composition and a dedicated Temporal task queue.

A deployment may serve multiple tenants only when those tenants intentionally share the exact same:

- Client-pack build and process definitions
- Adapter routing and credential-resolution implementation
- Model and policy configuration
- Release, upgrade, and rollback lifecycle

The deployment has an explicit tenant allow-list. Shared PostgreSQL and Temporal clusters remain permitted; PostgreSQL RLS and Activity authorization still enforce tenant isolation independently of deployment routing.

Client-pack imports are fixed by deployment configuration at startup. Requests, tenant records, events, and workflow history cannot select a Python import path. Tiramisu will not dynamically import a different pack inside workflow code or switch an active process to another deployment composition without an explicit compatible rollout or audited migration.

Each unit has a stable logical `deployment_id`. Each immutable release combines that ID with its build ID, canonical client-pack fingerprint, configured model ID, and Tiramisu version. The resulting SHA-256 release fingerprint deterministically owns one Temporal task queue. A process persists the logical deployment ID, release fingerprint, and task queue when it is created.

The configured tenant UUID allow-list and the tenant's durable database assignment must both name the running logical deployment. API authentication, worker startup, Activity authorization, action execution, and outbox dispatch all fail closed on disagreement. Assignment changes are attributed and audited.

## Upgrade, rollback, and migration rules

- A normal upgrade keeps the logical deployment ID and changes the immutable build, pack, model, or Tiramisu identity. It therefore receives a new release fingerprint and queue.
- Old and new workers may run together. Each dispatcher claims only outbox messages whose process pins match its release. Existing correlated processes remain on their original release; newly triggered processes use the API release that accepts them.
- Drain means retaining every release worker until its pinned processes are terminal and its deliveries are published. Deploying a new release does not mutate old process rows.
- Rollback routes new process creation back to a previously known release and restarts that release's API/worker artifact. Workers for processes created by the rolled-back release remain available until those processes drain.
- Changing files or model configuration under an unchanged build identity is prohibited. Build IDs identify reviewed immutable artifacts even though the release fingerprint also detects declared composition changes.
- Moving a whole tenant between logical deployments is allowed only after every existing process is terminal and every old-deployment outbox message is published. The trusted command records the actor and reason.
- Active-process migration is deliberately unsupported in the initial implementation. Never edit release pins or task queues directly. A future migration command must validate workflow and definition compatibility, preserve replay determinism, coordinate routing atomically, and write an immutable per-process audit record before this rule can change.

This cycle uses release-specific task queues rather than Temporal Worker Deployment Versioning. Adoption of Worker Deployment Versioning can be evaluated later, but it must preserve the process pins and fail-closed compatibility checks above.

A future shared control plane, operator UI, or ingress gateway may route a tenant to its deployment. It does not merge different pack runtimes into one worker.

## Consequences

- Client packs can be built, tested, deployed, scaled, upgraded, and rolled back independently while continuing to use one public Tiramisu core.
- Temporal task queues provide an explicit routing and operational boundary.
- A faulty or compromised pack has a smaller deployment blast radius, though shared infrastructure and the trusted-code model still require defense in depth.
- Clients using an identical product pack can share a deployment without requiring one deployment per tenant.
- The managed platform will eventually need deployment inventory, ingress routing, provider credential resolution, and richer rollout controls. Durable tenant assignment and immutable release identity are already application contracts.
- The API may initially be deployed with the pack. A generic shared ingress/control plane is a later service boundary, not a reason to make the current registry dynamic.
- Supporting several unrelated packs in one worker is outside the initial architecture. Reversing this decision requires a new ADR and evidence that the added routing complexity is operationally worthwhile.
