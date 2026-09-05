#!/usr/bin/env bash
# Local end-to-end test run for the communication-safety envelope.
# Requires: docker, uv, node/npm. Run from the repo root: ./scripts/test-local.sh
set -euo pipefail

export TIRAMISU_DATABASE_URL="${TIRAMISU_DATABASE_URL:-postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu}"
export TIRAMISU_MIGRATION_DATABASE_URL="${TIRAMISU_MIGRATION_DATABASE_URL:-postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu}"
export TIRAMISU_RUN_DB_TESTS=1

echo "==> Starting postgres + temporal"
docker compose up -d --wait postgres temporal

echo "==> Migrations"
uv run alembic upgrade head
uv run alembic check

echo "==> Backend lint + types"
uv run ruff check .
uv run ruff format --check .
uv run pyright

echo "==> Backend tests (expect 200 passed: 140 unit + 56 integration + 4 replay)"
uv run pytest

echo "==> Standalone support client project"
(
  cd examples/support_client_pack
  uv run --locked ruff check .
  uv run --locked pyright
  uv run --locked pytest
)

echo "==> Frontend"
(
  cd frontend
  [ -d node_modules ] || npm ci
  npm test
  npm run typecheck
  npm run build
)

echo "OK. Services left running; stop with: docker compose down -v"
