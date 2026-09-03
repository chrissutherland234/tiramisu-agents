# ADR-012: Opinionated client-project authoring

- Status: Accepted
- Date: 2026-09-04

## Context

The first client-pack contract was safe but too mechanical. An implementer had to keep a process
definition, extension manifest, policy IDs, action bindings, and a hand-written discriminated OpenAI
output union synchronized. That is valuable as a compiled runtime boundary, but it is not a good
authoring experience and would make a sub-afternoon client implementation unlikely.

A generic visual workflow builder or large YAML DSL would move complexity rather than remove it.
Client work includes typed provider parameters and bindings, so some reviewed code is expected. The
goal is the conceptual consistency of an opinionated web framework: one obvious place for each idea,
useful generated artifacts, fail-fast checks, and business-readable inspection.

## Decision

Adopt a code-first authoring framework with six public concepts:

- `Project` owns one client implementation.
- `Journey` declares goals, available capabilities, deterministic permissions and completion facts.
- `Route` maps a canonical business event to a journey start or wake.
- `Capability` binds a typed action parameter model to one provider-neutral adapter.
- `Fact` names typed authoritative knowledge or customer claims.
- `Scenario` expresses a reviewable example journey.

Compile those concepts into the existing immutable `ClientPack`. Derive the extension manifest,
process definitions, action bindings, policy identities, business metadata, and a strict
project-specific OpenAI output model. Retain the low-level ClientPack constructor as an advanced
escape hatch, not the normal path.

Provide `tiramisu startproject`, `tiramisu check`, and `tiramisu describe`. Keep projects as separate
editable Python packages until the distribution strategy is decided. Maintain a materially different
standalone example in public CI.

Completion requirements may reference only declared authoritative facts and are enforced both after
model proposal and while committing process state. `operator_editable` facts describe future typed
admin correction controls but do not themselves grant mutation authority. Model output excludes
standalone human wakes; human waits arise from deterministic approval and intervention state.

Use Python for typed boundaries, bindings, and narrow deterministic transforms. Do not turn journey
authoring into arbitrary Temporal workflow replacement. Defer a visual builder until several real
implementations demonstrate stable concepts. Scenario descriptions become the seed for a reusable
multi-layer runner, but their initial form is descriptive and compiler-validated.

## Consequences

The author maintains one source of truth and can inspect the compiled result in business language or
JSON. Capability parameter changes automatically change the model schema and pack fingerprint.
Routes and facts can later drive ingress guidance and typed operator forms without inventing another
configuration model.

The framework is intentionally opinionated and does not model every possible workflow topology.
Advanced deterministic behavior still requires reviewed Python. Client code remains trusted
executable code, and compilation does not sandbox adapters or prove model quality. Publication,
simulation, evaluation, provider credentials, generated ingress transforms, and the full typed fact
editor remain later lifecycle work.
