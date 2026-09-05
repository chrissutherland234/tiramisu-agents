# Token/cost budgets + circuit breakers — design plan

## Goal

Complete PLAN §18 milestone 1: durable model token/cost accounting and
budgets, plus tenant/platform/capability circuit breakers, following the
process-local communication envelope that just landed.

## Success Criteria

- Every model call's input/output tokens are recorded durably with a
  deterministic cost estimate; spend survives retries, restarts, and
  Continue-As-New.
- A process that exhausts its token or cost budget fails closed before the
  next model call with a non-retryable, operator-visible error; no further
  model spend is possible for that process.
- Tenant/platform spend fences trip automatically; capability and outbound
  breakers can be tripped/reset by operators with audit history.
- Kernel scenario driver and PostgreSQL/Temporal driver enforce identical
  budget rules (parity, as with communication policy).
- `docs/safety-limits.md`, `docs/testing.md` counts, and the PLAN
  autonomy-budget checkboxes reflect the new state.

## Context And Current Facts

- `AgentTurnRunner.run_turn` (`backend/src/tiramisu_agents/agents/runner.py`)
  returns a bare `AgentDecision`; SDK `Usage` (input/output/total tokens on
  `RunResult.context_wrapper.usage`, verified in
  `.venv/.../agents/run_context.py:83` and `agents/usage.py`) is discarded.
  One production caller (`temporal/activities/agent_turn.py:167`), two
  testkit runners (`testkit/scripted_agent.py`,
  `testkit/temporal_scenarios.py:123`), plus unit tests.
- The pre-model-call fence already exists:
  `_require_model_execution_allowed` runs before every attempt including
  corrections (`agent_turn.py:155-163`) and currently checks tenant status
  and process lifetime. Budget checks belong there.
- No usage/cost tables exist. `ProcessInstance`
  (`db/models/processes.py`) has no spend columns; PLAN data-model notes
  anticipate "budgets" on process rows and "usage, cost" audit records.
- Continue-As-New carries only mailbox buffers/counts
  (`temporal/workflows/mailbox.py:430-532`); anything in workflow memory is
  lost, so spend must live in PostgreSQL keyed by process — the same
  reasoning that made the action ledger the communication reservation
  ledger.
- Model name is free-form from settings (`openai_model`, `worker.py:69-112`);
  no price table exists anywhere.
- `TenantSafetyService.set_status` (`security/tenancy.py:81`) is the audited,
  row-locked transition pattern to reuse for breakers. Tenant suspension
  already pauses everything via `require_active_tenant`.
- `test_database_rls.py` audits every mapped tenant table's grants/policies,
  so a new ledger table must join the runtime-role grant contract and that
  test. Migration naming convention: `backend/alembic/versions/` (latest
  `20260905_15_runtime_role_hardening.py`).
- The communications envelope is the template: pure evaluator +
  PostgreSQL projection + enforcement at reservation and provider
  boundaries + kernel/Temporal driver parity + operator API/UI surface.

## Constraints And Non-goals

- Deterministic kernel stays free of SDK/provider imports: map SDK `Usage`
  to a kernel `ModelUsage` dataclass at the `openai_runner.py` boundary.
- Recorded rows are immutable; later price changes never rewrite history
  (store tokens + computed cost + price-table version per row).
- Non-goals: cross-process consent, recipient-specific timezones, raw
  transport caps, provider-response limits, real-model evals, per-turn
  prompt caps (already covered by `max_rendered_prompt_bytes`), live price
  feeds.

## Key Decisions

1. **Ledger table, not columns or workflow memory.** New immutable
   `model_usage_ledger` rows per (tenant, process, turn, attempt): model,
   input/output tokens, cost micros, price-table version. Unique key on
   (tenant, process, turn, attempt) makes Activity retries idempotent.
   Rejected: `ProcessInstance` spend columns (mutable, unaudited) and
   workflow counters (lost on Continue-As-New/restart).
2. **Runner returns usage with the decision.** Change `AgentTurnRunner` to
   return frozen `ModelTurnOutcome(decision, usage)`; update the OpenAI
   runner, `ScriptedAgent`, `_CompiledScenarioAgent`, and unit tests.
   Rejected: out-params/callbacks (hide a first-class result) and
   prompt-byte estimation (non-authoritative; SDK reports actuals).
3. **Cost from a versioned price table, fail-fast on unknown models.**
   `ModelPrices` ships defaults for known models, overridable via settings;
   worker composition refuses a model with no price entry (same fail-fast
   ethos as the missing-model check). Cost stored in integer micro-USD at
   record time. Rejected: tokens-only (PLAN demands cost) and NULL-cost
   rows for unknown models (silently unenforceable budgets).
4. **Process budgets in `ProcessLimits`; tenant/platform via breakers.**
   New `ProcessLimits` fields for max input/output/total tokens and max
   cost per process. Tenant spend fences are auto-tripping
   `model_calls`-scope evaluations from ledger sums. Capability/outbound/all
   breakers are manual, audited operator controls enforced in
   gateway/executor. Platform stays deployment kill switches
   (`platform_model_calls_paused`, `platform_outbound_messages_paused`):
   per-turn cross-tenant aggregation is incompatible with forced RLS on the
   app role, so platform spend aggregation is deferred to unit 6 docs as
   explicit remaining work.
5. **One pre-model-call fence, one exhaustion error.** Budget + breaker
   checks join `_require_model_execution_allowed`; exhaustion raises
   `ModelBudgetExceeded`, mapped to non-retryable `ApplicationError` into
   the existing intervention path (mirrors `ProcessLifetimeExceeded`).
6. **Scripted usage for driver parity.** `ScriptedAgent` decisions may
   declare usage (default zero); kernel `ScenarioRunner` accumulates and
   enforces with the same evaluator. Temporal driver keeps zero-usage
   compiled decisions but runs the same checkpoints.

## Recommended Approach

Mirror the communications implementation unit-for-unit: pure policy module
(`budgets/`), ledger service, migration + role grants, Activity fence +
recording, runner protocol change, driver parity, breaker service + operator
controls, API/UI spend surface, docs. Land budgets first (units 1–4),
breakers second (unit 5), visibility last (unit 6).

## Work Plan

1. **Kernel budget policy.** New `budgets/policy.py`: `ModelUsage`,
   `ModelBudget.from_definition`, pure `evaluate_model_budget(policy, spent)`
   returning an ordered block; `ModelBudgetExceeded`. `budgets/pricing.py`:
   `ModelPrices` + `estimate_cost_micros` + `PRICE_TABLE_VERSION`.
   `ProcessLimits`: max input/output/total tokens and max cost micros per
   process (defaults mirror message-budget conservatism). Pure unit tests
   incl. boundary/exact-limit cases.
2. **Ledger + migration.** `model_usage_ledger` table, Alembic migration 16
   with downgrade, explicit runtime-role grants (no default privileges, per
   `001_runtime_role.sql`), `ModelUsageService.record` (idempotent) and
   `spent` sums, RLS/grant test updates.
3. **Runner + Activity fence.** `ModelTurnOutcome` protocol change;
   OpenAI runner maps SDK usage; `ScriptedAgent`/`_CompiledScenarioAgent`
   updated; agent_turn records usage per attempt after each `run_turn` and
   checks budget/breakers before each attempt; `ModelBudgetExceeded`
   mapping; worker fail-fast on unpriced model.
4. **Driver parity + hardening tests.** No kernel-driver change: scripted
   scenarios make zero model calls, so token fences there would be theater
   (and would wrongly fail zero-cap journeys). Parity is the shared pure
   evaluator plus scripted-usage plumbing in `ScriptedAgent`, exercised
   through the real Activity. CaN survival holds by construction (ledger
   keyed by process, asserted via fresh-service inspect); exhaustion-blocks-
   turn, per-attempt recording, retry-idempotency integration tests.
5. **Circuit breakers.** `circuit_breakers` table (scope:
   `model_calls|outbound_messages|capability|all`; target; tripped state;
   audited transitions reusing the suspension pattern); auto-trip evaluation
   for tenant/platform `model_calls` spend; operator trip/reset API +
   enforcement in agent_turn, gateway, executor; capability breaker blocks
   reservation and execution of one action type.
6. **Visibility + docs.** `ProcessDetail` spend/budget summary + operator UI
   meter (mirrors communication-safety panel); update `safety-limits.md`,
   `testing.md` counts, PLAN checkboxes (§5 invariant 15 stays; §18 item 1
   and the autonomy-budget checkboxes flip), ADR-009 appendix if behavior
   diverges from the decision text.

## Validation Plan

- `uv run pytest backend/tests/unit/test_model_budgets.py` (new): evaluator
  boundaries, pricing math/versioning, unknown-model refusal.
- `TIRAMISU_RUN_DB_TESTS=1 uv run pytest
  backend/tests/integration/test_model_budget_enforcement.py` (new): ledger
  idempotency under Activity retry, exhaustion → non-retryable
  `ModelBudgetExceeded` with zero further provider calls, process-keyed CaN
  spend survival, tenant auto-trip, manual breaker trip/reset conflicts,
  reservation + execution breaker fences, operator trip/reset/list API.
- Full gates via `./scripts/test-local.sh` (200-test baseline grows; update
  `docs/testing.md`/`PLAN.md` counts from the actual run).
- Migration round-trip: `alembic downgrade`/`upgrade` across 16 plus
  `alembic check`, as CI does for 13/14.
- Manual: `tiramisu describe` on fictional pack shows budget lines;
  operator UI shows spend meter and tripped breaker state.

## Risks / Rollback

- Highest risk: the `run_turn` signature change ripples through runners and
  tests; a missed caller fails loudly at typecheck (`pyright`) — run it
  before tests.
- Price-table staleness underestimates new-model cost: mitigated by
  fail-fast on unknown models and immutable per-row price versions.
- Ledger write contention on hot processes: single-row INSERTs, no
  read-modify-write; spend reads are `SELECT sum` over an indexed
  (tenant, process) prefix.
- Rollback: migration 16 downgrades cleanly; fence checks are additive.
  Budget defaults must still bound spend; the chosen defaults will be
  stated in the implementation for review.

## Open Questions

None — all scope answers are in PLAN §18 item 1, ADR-009, and the schema
notes cited above. Price defaults for the configured models will be stated
in the implementation for review.
