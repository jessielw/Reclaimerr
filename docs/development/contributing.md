# Contributing

Reclaimerr accepts focused, understandable contributions. The goal is to keep
the codebase maintainable.

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
uv run uvicorn --reload --reload-dir backend backend.api.main:app
```

Run the frontend in a second terminal:

```bash
cd frontend
npm run dev
```

## Branching

- Base work on the `dev` branch unless the maintainers tell you otherwise.
- Target pull requests at `dev`.

## Architecture

Read the [architecture overview](architecture.md) before changing scheduler,
worker, or deletion behavior.

## Code Style

- Backend: `ruff check .` and `ruff format .`
- Frontend: `npm run format` and `npm run check`

## AI Contributions

AI can help with search, boilerplate, and debugging, but large AI-generated
submissions are not accepted.
