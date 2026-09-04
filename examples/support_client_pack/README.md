# Support client pack

This is a separately installable, editable Tiramisu project for a business that manages
customer support cases. It is intentionally unrelated to the bundled booking example.

It demonstrates the conventional authoring surface:

- `Project`: the client implementation;
- `Journey`: one support case from creation to authoritative resolution;
- `Route`: `case.created` starts it; customer replies and provider resolution wake it;
- `Capability`: a typed, approval-gated customer reply using a replaceable adapter;
- `Fact`: a typed case status which also describes a future operator fact form;
- `Scenario`: an executable happy path in language a business owner can review.

The package uses an editable path to this repository while Tiramisu is pre-PyPI:

```console
uv sync --project examples/support_client_pack --all-groups
uv run --project examples/support_client_pack tiramisu check support_client_pack:create_project
uv run --project examples/support_client_pack tiramisu describe support_client_pack:create_project
uv run --project examples/support_client_pack tiramisu simulate support_client_pack:create_project --scenario answer_then_resolve
uv run --project examples/support_client_pack pytest
```

The deployment entry point is `support_client_pack:create_client_pack`. When replacing
`StubActionAdapter` with a real messaging adapter, retain a separate explicitly safe
`simulation_adapter` so the acceptance scenario cannot send a real message.
