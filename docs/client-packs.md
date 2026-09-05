# Building a client project

Tiramisu client work is ordinary, version-controlled Python in a separate editable package. The
recommended surface is deliberately small and conventional: describe the business in seven concepts,
then let Tiramisu compile the low-level manifest, process definition, policy registrations, strict
OpenAI output model, and runtime bindings.

| Concept | Business meaning | Web-framework analogy |
| --- | --- | --- |
| `Project` | One client implementation and deployment unit | Site/project settings |
| `Journey` | The goal and rails for one long-running relationship | View/controller |
| `Route` | A business event that starts or wakes a journey | URL route |
| `Capability` | A typed business operation backed by an adapter | Service/form action |
| `Fact` | Named, typed business knowledge and its authority class | Domain model field |
| `Communications` | Which actions contact customers and when contact is forbidden | Middleware/policy |
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
tiramisu simulate acme_service:create_project --scenario happy_path
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
from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.projects import (
    Capability,
    Communications,
    DailyQuietHours,
    Fact,
    Journey,
    ProcessLimits,
    Project,
    Route,
)


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
        limits=ProcessLimits(
            max_outbound_messages_per_process=20,
            max_outbound_messages_per_window=3,
            outbound_message_window_hours=24,
            maximum_process_lifetime_days=60,
        ),
        communications=Communications(
            outbound_actions=(send_reply.action_type,),
            customer_reply_events=("customer.email_received",),
            opt_out_events=("customer.email_opted_out",),
            automated_response_events=("customer.email_auto_replied",),
            quiet_hours=DailyQuietHours(
                timezone="Pacific/Auckland",
                start_local=time(20),
                end_local=time(8),
            ),
        ),
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
                "customer.email_received",
                journey=journey.id,
                title="Customer replied",
                description="Wake when a genuine customer reply arrives.",
            ),
            Route.wake(
                "customer.email_opted_out",
                journey=journey.id,
                title="Customer opted out",
                description="Permanently stop outbound contact for this journey.",
            ),
            Route.wake(
                "customer.email_auto_replied",
                journey=journey.id,
                title="Automated response received",
                description="Stop outbound contact until a later human reply.",
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

`Communications` is also compiled into deterministic policy. The model cannot relabel an event,
ignore quiet hours, spend beyond a message budget, or make an approval override an opt-out. Provider
adapters must classify genuine replies, opt-outs, and automated responses into the declared canonical
event types. Tiramisu then derives usage from the durable event/action ledgers and checks it when an
action is reserved and again immediately before provider execution.

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
- Communications classifies customer-contact actions and inbound event roles. Quiet hours currently
  use one IANA timezone per journey. Opt-out is enforced for the current process; a future shared
  customer consent registry is still required before one person can be contacted across multiple
  independent processes or channels.
- A `decision_transformer` is an advanced, versioned escape hatch for a small deterministic
  transition after structured output conversion. It cannot bypass the downstream policy or action
  gateway. Most projects should not need one.
- Scenarios are executable, business-readable acceptance specifications. Event steps supply typed
  integration facts, action steps are deterministic scripted agent decisions, wait steps assert the
  wake plan, fact steps assert the resulting projection, and completion still passes through the
  production decision and lifecycle rules. They are intentionally not a visual workflow DSL.

## Executing a scenario

An executable scenario uses the same names as the journey rather than recreating its lifecycle:

```python
from tiramisu_agents.projects import Scenario, ScenarioStep, ScenarioValue

Scenario(
    id="happy_path",
    journey_id="support_case",
    title="Answer and resolve a case",
    description="A reviewed reply is sent before authoritative resolution.",
    steps=(
        ScenarioStep.event(
            "case.created",
            "A customer opens a case.",
            facts=(CASE_STATUS.observed("open"),),
        ),
        ScenarioStep.action(
            "send_reply",
            "An operator approves the reply.",
            parameters={
                "recipient": ScenarioValue.fact(CUSTOMER_EMAIL),
                "body": "We are looking into this.",
            },
            approve=True,
        ),
        ScenarioStep.wait_for_event(
            "case.resolved", "The agent sleeps until the support system resolves the case."
        ),
        ScenarioStep.event(
            "case.resolved",
            "The support system resolves the case.",
            facts=(CASE_STATUS.observed("resolved"),),
        ),
        ScenarioStep.fact(CASE_STATUS, "resolved", "Resolution is authoritative."),
        ScenarioStep.complete("The relationship agent completes."),
    ),
)
```

`ScenarioValue.fact(...)` reads a fact established by an earlier event or action result; an optional
path such as `ScenarioValue.fact(AVAILABLE_SLOTS, 0)` selects a nested value. Timer waits use
`ScenarioStep.wait_for_timer(...)` and advance the deterministic scenario clock instantly.

Run a named scenario with no PostgreSQL, Temporal, OpenAI, network, or credentials:

```console
tiramisu simulate acme_service:create_project --scenario happy_path
tiramisu simulate acme_service:create_project --scenario happy_path --json
```

The runner validates scripted decisions through the generated strict output schema and production
decision policy, classifies actions through the production permission and communication policies,
derives exact action identities, calls only explicitly safe simulation adapters, and projects facts,
process status, wakes, and completion through the same infrastructure-free transition functions
used by PostgreSQL persistence. Its fake clock exercises quiet hours, rolling windows, follow-up
resets, and process lifetime without sleeping. Its trace shows every event, decision, approval,
provider result, wait, fact assertion, and completion.

Integration suites can pass the same pack to `PostgresTemporalScenarioDriver`. That driver creates
an isolated test tenant, sends event steps through real ingestion and outbox delivery, runs the
real Temporal mailbox and Activities, applies authored approvals through the review service,
executes only `simulation_bindings`, and checks durable facts, actions, approvals, wake records,
audit revisions, and completion. It restarts the worker composition at external checkpoints and
uses Temporal time skipping for timer steps. This is the slower cross-layer acceptance test; keep
the integration-free driver as the default author feedback loop.

If a capability's deployment adapter is real, bind a separate safe adapter for scenarios:

```python
Capability(
    # ...
    adapter=real_email_adapter,
    simulation_adapter=StubActionAdapter(),
)
```

Stub adapters supplied by Tiramisu are marked as simulation-safe. `simulate` refuses to use an
ordinary production binding merely because it implements the same methods.
Scenario fixtures must be synthetic and safe to retain in version control; do not embed client
credentials or real customer content in compiled project metadata or traces.

Use a real adapter only after its side-effect and reconciliation behavior fits the public
`ActionAdapter` contract. An adapter ID is an immutable release identity: change it when provider
behavior changes incompatibly.

## Inspecting the compiled artifact

```console
tiramisu check acme_service:create_project
tiramisu describe acme_service:create_project
tiramisu describe acme_service:create_project --json
tiramisu simulate acme_service:create_project --scenario happy_path
```

`check` constructs the complete pack and validates the generated OpenAI schema. `describe` prints
routes, capabilities, permissions, completion facts, and scenarios. JSON output exposes the compiled
business metadata for future tooling and admin forms. `simulate` executes one scenario using its
safe adapter bindings and returns a readable or JSON trace.

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
