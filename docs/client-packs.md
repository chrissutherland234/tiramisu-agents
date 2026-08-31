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

## Current boundary

- One configured composition is loaded per API or worker process. A pack may contain multiple non-conflicting definitions, but they currently share one strict decision-output type and one action-binding namespace.
- The deployment, not a request or tenant-controlled field, chooses the import path. Treat the package as executable production code and pin its immutable build.
- Custom Temporal Activity registration, dynamic lifecycle controls, persisted installation audit, and per-tenant adapter routing within one worker are not yet supported.
- A pack cannot add database migrations or bypass the action gateway, tenant checks, approval integrity, or core audit path through this contract.

See [`examples/fictional_client_pack`](../examples/fictional_client_pack/README.md) for the buildable editable-package example.
