# Contributing

Tiramisu is early-stage. Please open an issue before making a large architectural change.

## Development

1. Install `uv`, Python 3.13 or 3.14, Node.js, and npm.
2. Run `uv sync --all-groups`.
3. Run `uv run ruff check .`, `uv run pyright`, and `uv run pytest`.
4. In `frontend`, run `npm install`, `npm run typecheck`, and `npm run build`.

Keep the pure kernel free of network, database, Temporal, OpenAI, and provider dependencies. Side effects belong behind ports and in Activities. New adapters must pass the shared contract suite.

Never commit customer information, production payloads, credentials, private client prompts, or proprietary client process definitions. Use synthetic fixtures.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
