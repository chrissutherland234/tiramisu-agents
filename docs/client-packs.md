# Client-pack composition

A client pack is ordinary trusted Python code installed beside Tiramisu. It exposes one zero-argument factory that returns the public `tiramisu_agents.extensions.ClientPack` contract. The deployment configures that same factory path for its API and worker.

```python
from tiramisu_agents.extensions import ClientPack


def create_client_pack() -> ClientPack:
    return ClientPack(
        manifest=manifest,
        definitions=(definition,),
        bindings={
            "send_message": messaging_adapter,
            "create_booking": booking_adapter,
        },
        agent_decision_output_type=StrictClientDecisionOutput,
        policy_ids=("client.default.v1",),
    )
```

The manifest's process, adapter, and policy identities must exactly match the runtime objects. Binding keys are action types from the definitions; adapter objects expose the provider-neutral `ActionAdapter` protocol. The output type is a strict Pydantic model with `to_agent_decision(turn_input)` conversion, keeping provider-shaped parameters outside the deterministic kernel contract.

Install both projects in the same environment during local development:

```bash
uv pip install -e /path/to/tiramisu -e /path/to/client-pack
export TIRAMISU_CLIENT_PACK_FACTORY=client_package:create_client_pack
```

API startup installs trigger rules and the definition registry. Worker startup installs that same registry plus the strict output model and adapter bindings. Both require the logical deployment ID, immutable build ID, model ID, and tenant allow-list; only the worker requires `OPENAI_API_KEY`. Loading is explicit and fail-fast; Tiramisu performs no entry-point scanning, directory discovery, or workflow-time import.

## Publication and compatibility fence

Only definitions with `status: published` install production trigger rules. Draft and retired definitions may remain in a pack for validation or compatibility purposes, but the current runtime has no draft simulation mode and will not start a real process from them.

At startup, `ClientPack.fingerprint()` hashes a canonical composition containing:

- The extension manifest and all process definitions
- The strict agent-output Python type identity and generated JSON Schema
- Registered policy identities
- Every action-type-to-adapter identity and its idempotency guarantee

Process creation persists that pack fingerprint, the existing extension-manifest hash, and the exact definition fingerprint. The worker compares all three with its startup composition while loading agent context and again at provider authorization. Any mismatch is non-retryable, performs no model or provider I/O, and leaves the mailbox waiting on a durable operator-visible intervention.

Process creation also persists the stable logical deployment ID, immutable release fingerprint, and derived Temporal task queue. The release fingerprint covers the build ID, pack fingerprint, model ID, and Tiramisu version. API and worker health output exposes this identity for deployment verification. Outbox dispatch joins each message to its process pins, so old and new release workers can coexist without claiming each other's work.

The fingerprint describes declared composition, not arbitrary Python source. Treat extension versions, policy IDs, and adapter IDs as immutable release identities and deploy reviewed immutable artifacts. If their implementation or behavior changes, publish a new identity or version even when a Python class name is unchanged.

Migration `20260901_12` cannot reconstruct historical pack composition. It marks pre-existing process rows with an all-zero unverified pack fingerprint. Migration `20260901_13` likewise assigns historical tenants and process releases to `unassigned` and gives old release fingerprints an all-zero sentinel. Those values intentionally never match a real managed release. Historical processes require their original artifact plus an explicit future audited compatibility migration; do not replace sentinels by assumption.

## Release operations

Keep `TIRAMISU_DEPLOYMENT_ID` stable for one logical client-pack service. Change `TIRAMISU_DEPLOYMENT_BUILD_ID` for every immutable artifact. A pack, model, build, or Tiramisu version change derives a new queue automatically. Run the previous and next workers concurrently until processes pinned to the previous release drain.

Rollback restores a previously known API/worker artifact for new process creation. It does not convert processes created by the newer release, so that release's worker must also drain them. Incoming events correlated to an existing process may pass through either API release: outbox delivery follows the process pin, not the receiving API.

Assign a tenant to the logical deployment with `tiramisu-admin assign-tenant-deployment`; do not update the database directly. A tenant cannot move to another logical deployment while nonterminal processes or outstanding deliveries exist. Active-process release migration is not currently supported and direct edits to process release fingerprints or queues are unsafe.

## Current boundary

- One client pack is one independently deployable API/worker composition with its own Temporal task queue. It may serve multiple tenants only when they intentionally share the exact pack, adapter routing, model/policy configuration, and release lifecycle. See [ADR-011](decisions/011-client-pack-deployment-topology.md).
- The deployment, not a request or tenant-controlled field, chooses the import path. Treat the package as executable production code and pin its immutable build.
- Custom Temporal Activity registration, dynamic enable/disable, persisted installation inventory, active-process migration, and per-tenant adapter routing within one worker are not yet supported.
- The supported contract cannot register replacement workflows or bypass the stock action gateway, tenant checks, approval integrity, or audit path. The pack itself is trusted executable Python; malicious or careless code can still perform hidden I/O. Use reviewed immutable builds, and use a separate process/service boundary where code-level isolation is required.
- A fingerprint mismatch deliberately stops existing processes. Safe upgrade and rollback keep every required pinned release worker available until drain; changing files in place under an existing build identity is not supported.

The [bundled fictional pack](../backend/src/tiramisu_agents/builtin/fictional.py) is the repository's local reference composition. A downstream pack should keep its definitions, manifest, output contract, policies, and bindings together in its own package.
