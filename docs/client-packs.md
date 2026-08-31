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

API startup installs trigger rules and the definition registry. Worker startup installs that same registry plus the strict output model and adapter bindings, and also requires `TIRAMISU_OPENAI_MODEL` and `OPENAI_API_KEY`. Loading is explicit and fail-fast; Tiramisu performs no entry-point scanning, directory discovery, or workflow-time import.

## Publication and compatibility fence

Only definitions with `status: published` install production trigger rules. Draft and retired definitions may remain in a pack for validation or compatibility purposes, but the current runtime has no draft simulation mode and will not start a real process from them.

At startup, `ClientPack.fingerprint()` hashes a canonical composition containing:

- The extension manifest and all process definitions
- The strict agent-output Python type identity and generated JSON Schema
- Registered policy identities
- Every action-type-to-adapter identity and its idempotency guarantee

Process creation persists that pack fingerprint, the existing extension-manifest hash, and the exact definition fingerprint. The worker compares all three with its startup composition while loading agent context and again at provider authorization. Any mismatch is non-retryable, performs no model or provider I/O, and leaves the mailbox waiting on a durable operator-visible intervention.

The fingerprint describes declared composition, not arbitrary Python source. Treat extension versions, policy IDs, and adapter IDs as immutable release identities and deploy reviewed immutable artifacts. If their implementation or behavior changes, publish a new identity or version even when a Python class name is unchanged.

Migration `20260901_12` cannot reconstruct historical pack composition. It marks pre-existing process rows with an all-zero unverified fingerprint, which intentionally never matches a real SHA-256 pack fingerprint. Those processes require a future explicit, audited compatibility migration or their original deployment artifact; do not replace the sentinel by assumption.

## Current boundary

- One client pack is one independently deployable API/worker composition with its own Temporal task queue. It may serve multiple tenants only when they intentionally share the exact pack, adapter routing, model/policy configuration, and release lifecycle. See [ADR-011](decisions/011-client-pack-deployment-topology.md).
- The deployment, not a request or tenant-controlled field, chooses the import path. Treat the package as executable production code and pin its immutable build.
- Custom Temporal Activity registration, dynamic lifecycle controls, persisted installation audit, audited active-process migration, and per-tenant adapter routing within one worker are not yet supported.
- The supported contract cannot register replacement workflows or bypass the stock action gateway, tenant checks, approval integrity, or audit path. The pack itself is trusted executable Python; malicious or careless code can still perform hidden I/O. Use reviewed immutable builds, and use a separate process/service boundary where code-level isolation is required.
- A fingerprint mismatch deliberately stops existing processes. Safe upgrade and rollback therefore require the old compatible deployment to remain available or a future audited active-instance migration; changing files in place under an existing release identity is not supported.

See [`examples/fictional_client_pack`](../examples/fictional_client_pack/README.md) for the buildable editable-package example.
