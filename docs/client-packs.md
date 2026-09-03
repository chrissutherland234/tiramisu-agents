# Building a client project

Tiramisu client work is ordinary, version-controlled Python in a separate editable package. The
recommended surface is deliberately small and conventional: describe the business in six concepts,
then let Tiramisu compile the low-level manifest, process definition, policy registrations, strict
OpenAI output model, and runtime bindings.

| Concept | Business meaning | Web-framework analogy |
| --- | --- | --- |
| `Project` | One client implementation and deployment unit | Site/project settings |
| `Journey` | The goal and rails for one long-running relationship | View/controller |
| `Route` | A business event that starts or wakes a journey | URL route |
| `Capability` | A typed business operation backed by an adapter | Service/form action |
| `Fact` | Named, typed business knowledge and its authority class | Domain model field |
| `Scenario` | A reviewable example of how the journey should unfold | Acceptance test |

This is code-first rather than a general workflow language. Python is useful for provider bindings,
typed parameters, and the occasional deterministic transition. The common path remains declarative
enough that a business owner can review the output of `tiramisu describe` without understanding the
Temporal workflow.

## Start with the convention

Create a separate project, install it alongside this repository while the public package name is
still deferred, then compile it:

```console
uv run tiramisu startproject acme_service ../acme-service
cd ../acme-service
uv venv
uv pip install -e /path/to/tiramisu
uv pip install -e .
tiramisu check acme_service:create_project
tiramisu describe acme_service:create_project
pytest
```

The generated package has one obvious definition module, a zero-argument deployment factory, a
business-readable scenario, and a test. It uses a stub adapter until the provider binding is
replaced. Tiramisu refuses to scaffold over a non-empty directory.

The repository's
[standalone support example](../examples/support_client_pack/README.md) is a complete second package
with its own editable dependency, lockfile, tests, and CI gate. The
[bundled booking project](../backend/src/tiramisu_agents/builtin/fictional.py) is the richer local
demo.

## A minimal definition

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.projects import Capability, Fact, Journey, Project, Route


class SendReplyParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str
    body: str


CASE_STATUS = Fact(
    key="case.status",
    title="Case status",
    description="The status reported by the support system.",
    value_type=Literal["open", "resolved"],
    operator_editable=True,
)


def create_project() -> Project:
    send_reply = Capability(
        action_type="send_reply",
        title="Send a reply",
        description="Reply through the configured messaging provider.",
        parameters_model=SendReplyParameters,
        adapter=StubActionAdapter(),
        guidance="Use the verified customer address and customer-ready plain text.",
    )
    journey = Journey(
        id="support_case",
        version="1",
        title="Resolve one support case",
        description="Stay with the customer until the case is resolved.",
        goals=("Resolve the customer's problem",),
        capabilities=(send_reply.action_type,),
        complete_when=(CASE_STATUS.equals("resolved"),),
    )
    return Project(
        id="acme_service",
        version="0.1.0",
        title="Acme service",
        description="Acme's durable support journeys.",
        journeys=(journey,),
        routes=(
            Route.start(
                "case.created",
                journey=journey.id,
                title="Case created",
                description="Start an agent for each new case.",
                provides=(CASE_STATUS,),
            ),
            Route.wake(
                "case.resolved",
                journey=journey.id,
                title="Case resolved",
                description="Wake when the support provider resolves the case.",
                provides=(CASE_STATUS,),
            ),
        ),
        capabilities=(send_reply,),
        facts=(CASE_STATUS,),
    )


def create_client_pack():
    return create_project().compile()
```

`Project.compile()` is fail-fast. It rejects missing start routes, unknown capabilities, undeclared
wake routes, inconsistent facts, unsafe completion claims, invalid scenarios, incomplete adapter
bindings, and ambiguous published triggers. It generates a discriminated action union from each
capability's strict Pydantic parameter model and restricts model-selected event wakes to the declared
wake routes. Human approval wakes cannot be invented by the model; they arise from deterministic
permission policy.

Completion is also a deterministic fact gate. If the model proposes `completed`, both the agent-turn
validator and the persistence boundary require every `complete_when` value to match the current
authoritative fact projection. A customer message, model summary, or operator Wake instruction
cannot manufacture resolution or payment.

## Where business logic belongs

- Routes map canonical events to starts and wakes. Webhook verification and transformation into a
  `CanonicalEvent` remain integration concerns.
- Capabilities declare provider-neutral action names, exact parameters, guidance, default
  permission, produced facts, and the adapter that performs the operation.
- Facts describe values emitted by trusted event or adapter boundaries. Declaring a fact does not
  make arbitrary text authoritative. `operator_editable=True` is metadata for a future typed,
  audited fact-correction UI; that UI is not implemented yet.
- Journey guidance helps the model choose among allowed proposals. Hard lifecycle, permission,
  completion, idempotency, and authority rules stay deterministic.
- A `decision_transformer` is an advanced, versioned escape hatch for a small deterministic
  transition after structured output conversion. It cannot bypass the downstream policy or action
  gateway. Most projects should not need one.
- Scenarios are business-readable specifications today. The next testing increment will make the
  same scenario data executable at multiple runtime layers; they are not yet a visual workflow DSL.

Use a real adapter only after its side-effect and reconciliation behavior fits the public
`ActionAdapter` contract. An adapter ID is an immutable release identity: change it when provider
behavior changes incompatibly.

## Inspecting the compiled artifact

```console
tiramisu check acme_service:create_project
tiramisu describe acme_service:create_project
tiramisu describe acme_service:create_project --json
```

`check` constructs the complete pack and validates the generated OpenAI schema. `describe` prints
routes, capabilities, permissions, completion facts, and scenarios. JSON output exposes the compiled
business metadata for future tooling and admin forms.

The compiler still produces the stable `tiramisu_agents.extensions.ClientPack` runtime contract.
Advanced packages may construct that low-level contract directly, but then they own the manifest,
definition, policy IDs, bindings, and strict output conversion themselves. The conventional surface
is the supported golden path.

## Deployment and compatibility fence

Configure the exact same zero-argument compiled-pack factory for API and worker:

```dotenv
TIRAMISU_CLIENT_PACK_FACTORY=acme_service:create_client_pack
```

One client pack is one independently deployable API/worker composition with its own release-derived
Temporal task queue. Several tenants may share it only when they intentionally share the exact
project, adapter routing, model/policy configuration, and release lifecycle.

At startup, `ClientPack.fingerprint()` hashes the manifest, definitions, generated output type and
JSON Schema, policy identities, action bindings, and business metadata. Process creation persists
the pack and definition fingerprints plus its release and queue. Incompatible workers stop before
model or provider I/O. Old and new release workers coexist until their pinned processes drain.

Installed client code is trusted executable Python, not a sandbox. Keep real client projects in
reviewed private repositories, never as canonical source in a gitignored folder. Pin immutable
artifacts for deployment, keep credentials in a secret manager, and use a separate service boundary
where protection from malicious pack code is required. Custom Temporal Activities, dynamic
tenant-selected imports, active-process migration, and persisted installation inventory remain
outside the current contract.
