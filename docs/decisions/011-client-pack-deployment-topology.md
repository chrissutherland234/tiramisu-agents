# ADR-011: Client-pack deployment topology

- Status: Accepted
- Date: 2026-08-31

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

A future shared control plane, operator UI, or ingress gateway may route a tenant to its deployment. It does not merge different pack runtimes into one worker.

## Consequences

- Client packs can be built, tested, deployed, scaled, upgraded, and rolled back independently while continuing to use one public Tiramisu core.
- Temporal task queues provide an explicit routing and operational boundary.
- A faulty or compromised pack has a smaller deployment blast radius, though shared infrastructure and the trusted-code model still require defense in depth.
- Clients using an identical product pack can share a deployment without requiring one deployment per tenant.
- The managed platform will eventually need deployment inventory, tenant-to-deployment routing, immutable artifact identity, provider credential resolution, and rollout controls.
- The API may initially be deployed with the pack. A generic shared ingress/control plane is a later service boundary, not a reason to make the current registry dynamic.
- Supporting several unrelated packs in one worker is outside the initial architecture. Reversing this decision requires a new ADR and evidence that the added routing complexity is operationally worthwhile.
