# ADR-010: Public core and private client packs

- Status: Accepted
- Date: 2026-08-29

## Context

Tiramisu should be useful as a public MIT-licensed project without exposing client IP, data, or credentials.

## Decision

Keep the generic kernel, orchestration, API, Vue app, migrations, test kit, stubs, reusable adapters, and fictional examples public under MIT. Keep real client processes, prompts, policies, proprietary adapters, evaluations, and deployment composition in separate private repositories and packages.

Compose deployments through an explicit versioned extension manifest at worker startup. Private packs pin Tiramisu and pass public contracts. The supported registration surface may narrow authority and does not replace the workflow or action gateway.

An installed Python pack is trusted executable deployment code, not a security sandbox. Its factory, output conversion, and adapters can execute arbitrary Python, so source review, immutable builds, dependency controls, and deployment isolation are required. Protection from a malicious pack requires a separate process/service boundary. Do not store canonical client work only in a gitignored directory.

Begin with one editable package using the `tiramisu_agents` import namespace. Defer PyPI naming and publication until the extension API is stable.

## Consequences

Public CI has no private dependency. Local client development uses sibling editable packages; CI and deployments use immutable revisions or artifacts. MIT permits commercial private forks and hosted derivatives. Client-pack compatibility is a runtime and release concern; Python protocols alone do not establish isolation.
