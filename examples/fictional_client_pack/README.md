# Fictional client pack

This directory demonstrates the shape and registration path required of a separate private client package. It contains no real customer data or proprietary configuration.

The public manifest and process definition are mirrored into `tiramisu_agents.builtin` so they are present in editable installs and wheels. A contract test rejects drift between the public example and those runtime package resources.

This directory is also an independently installable package that demonstrates the supported startup boundary. Install the Tiramisu repository and this package in editable mode, then point both the API and worker at its explicit factory:

```bash
uv pip install -e . -e examples/fictional_client_pack
export TIRAMISU_CLIENT_PACK_FACTORY=tiramisu_fictional_client_pack:create_client_pack
```

The tiny sample factory delegates to the canonical bundled demo to avoid maintaining a third copy. A real downstream package returns `tiramisu_agents.extensions.ClientPack` constructed from its own packaged definitions, strict output model, policies, and adapter bindings. Factory import and execution happen only at API/worker startup, never from deterministic Temporal workflow code.
