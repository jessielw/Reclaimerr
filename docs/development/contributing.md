# Contributing

Reclaimerr accepts focused, understandable contributions. The goal is to keep the codebase maintainable.

## Development Setup

**Requirements:** Python 3.11+, Node.js 20+, and `uv`

```bash
git clone https://github.com/jessielw/Reclaimerr.git
cd Reclaimerr
uv sync
cd frontend
npm install
cd ..
```

Run the backend:

```bash
uv run uvicorn --no-proxy-headers --reload --reload-dir backend backend.api.main:app
```

Run the frontend in a second terminal:

```bash
cd frontend
npm run dev
```

## Branching

- Base work on the `dev` branch unless the maintainers tell you otherwise.
- Target pull requests at `dev`.
- User-facing changes get a line under `## [Unreleased]` in `CHANGELOG.md` (Added / Changed / Fixed). Maintainers move these under the version heading at release time, and the release notes workflow publishes them. See [Release Process](release-process.md).

## Architecture

Read the [architecture overview](architecture.md) before changing scheduler, worker, or deletion behavior.

## Code Style

- Backend: `ruff check .` and `ruff format .`
- Frontend: `npm run format` and `npm run check`

CI runs `ruff check` and `ruff format --check`, so run `ruff format .` (not just `ruff check`) before pushing. Use `ruff format --check .` to verify formatting the same way CI does, without modifying files.

## Sharing Rules

Rules can be shared without writing code. See [Community Rules](../community-rules/index.md#sharing-a-rule) for the steps.

## Desktop

- To run desktop in development use `uv run --extra desktop python -m desktop`

## AI Contributions

AI can help with search, boilerplate, and debugging, but large AI-generated submissions are not accepted.
