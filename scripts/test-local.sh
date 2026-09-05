#!/usr/bin/env bash
# Run from any directory. Full checks use a dedicated local test database.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
trap 'code=$?; echo "Test run failed at line ${LINENO} (exit ${code})." >&2; exit "$code"' ERR

mode="${1:-all}"
case "$mode" in
  quick|backend|frontend|all) ;;
  -h|--help)
    cat <<'HELP'
Usage: bash scripts/test-local.sh [quick|backend|frontend|all]
  quick     Backend unit tests and frontend component tests; no Docker.
  backend   Backend lint/types, full PostgreSQL/Temporal suite, client tests.
  frontend  Frontend component tests, typecheck and production build.
  all       backend + frontend (default).

Full backend checks start an isolated PostgreSQL on localhost:15432.
Set TIRAMISU_TEST_POSTGRES_PORT to choose another port.
Temporal tests start their own time-skipping server; no dev server is needed.
HELP
    exit 0 ;;
  *) echo "Unknown mode: $mode. Use --help." >&2; exit 2 ;;
esac
if (( $# > 1 )); then echo "Expected one mode. Use --help." >&2; exit 2; fi

if [[ "$mode" != frontend ]]; then
  command -v uv >/dev/null || { echo "Install uv first." >&2; exit 1; }
  uv sync --locked --all-groups
fi
if [[ "$mode" == quick ]]; then
  echo "==> Backend unit tests"
  TIRAMISU_RUN_DB_TESTS=0 uv run --locked pytest backend/tests/unit -q
elif [[ "$mode" == backend || "$mode" == all ]]; then
  echo "==> Starting isolated test PostgreSQL"
  docker compose -f compose.test.yaml up -d --wait postgres
  test_port="${TIRAMISU_TEST_POSTGRES_PORT:-15432}"
  export TIRAMISU_DATABASE_URL="postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:${test_port}/tiramisu_test"
  export TIRAMISU_MIGRATION_DATABASE_URL="postgresql+asyncpg://tiramisu:tiramisu@localhost:${test_port}/tiramisu_test"
  export TIRAMISU_RUN_DB_TESTS=1

  echo "==> Migrations"
  uv run --locked alembic upgrade head
  uv run --locked alembic check
  echo "==> Backend lint + types"
  uv run --locked ruff check .
  uv run --locked ruff format --check .
  uv run --locked pyright
  echo "==> Backend tests"
  uv run --locked pytest
  echo "==> Standalone support client project"
  (
    cd examples/support_client_pack
    uv sync --locked --all-groups
    uv run --locked ruff check .
    uv run --locked pyright
    uv run --locked pytest
  )
fi
if [[ "$mode" != backend ]]; then
  echo "==> Frontend"
  (
    cd frontend
    npm ci --no-audit --no-fund
    npm test
    if [[ "$mode" != quick ]]; then npm run build; fi
  )
fi

echo "OK."
if [[ "$mode" == backend || "$mode" == all ]]; then
  echo "Test database left running. Stop with: docker compose -f compose.test.yaml down"
fi
