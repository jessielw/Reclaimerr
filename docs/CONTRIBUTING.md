# Contributing to Reclaimerr

## On AI Generated Contributions

Reclaimerr was built by hand and I intend to keep it that way. Using AI as a tool (searching docs, debugging, repetitive boilerplate) is fine, but **pull requests that are clearly large blocks of AI-generated content will not be accepted**. If I can tell you didn't understand what you submitted, it won't be merged.

## Development Setup

**Requirements:** Python 3.11+, Node.js 20+, [uv](https://docs.astral.sh/uv/)

1. Clone the repository

   ```bash
   git clone https://github.com/jessielw/Reclaimerr.git
   cd Reclaimerr
   ```

2. Install dependencies

   ```bash
   uv sync
   cd frontend && npm install && cd ..
   ```

3. Create your environment file and fill in the required values

   ```bash
   cp docker/.env.example .env
   ```

4. Start the backend (from repo root)

   ```bash
   uv run uvicorn --reload --reload-dir backend backend.api.main:app
   ```

5. Start the frontend (in a second terminal)
   ```bash
   cd frontend && npm run dev
   ```

Backend runs at `http://localhost:8000`, frontend at `http://localhost:3000`.

## Code Style

- **Backend:** [Ruff](https://docs.astral.sh/ruff/) for linting and formatting - `ruff check .` and `ruff format .`
- **Frontend:** [Prettier](https://prettier.io/) - `npm run format` and [svelte-check](https://github.com/sveltejs/language-tools) - `npm run check`

The CI workflows enforce both on every pull request.

## Database Migrations

Reclaimerr uses [Alembic](https://alembic.sqlalchemy.org/) for database schema migrations. Migrations run automatically on every startup so users never need to run anything manually.

### If your PR changes a model in `backend/database/models.py`

Generate a migration after making your changes:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Review the generated file in `backend/alembic/versions/` — autogenerate is not always perfect (it can't detect renamed columns, for instance). Commit the migration file with your PR.

## Releasing a New Version

> **This section is for maintainers only.** If you're opening a PR, you don't need to worry about any of this.

Tags drive all release automation (Docker image push, desktop builds). Tags must **not** have a `v` prefix - use `0.1.0`, not `v0.1.0`.

### Version files - update all four

| File                          |
| ----------------------------- |
| `pyproject.toml`              |
| `__version__.py`              |
| `frontend/package.json`       |
| `frontend/src/lib/version.ts` |

### Release checklist

- [ ] Update the three version files above
- [ ] Run `uv sync` to update the lock file
- [ ] Run `ruff check .` and `ruff format .`
- [ ] Run `cd frontend && npm run check && npm run format`
- [ ] Smoke test backend and frontend locally
- [ ] Commit: `git commit -m "chore: bump version to X.Y.Z"`
- [ ] Tag (no `v` prefix): `git tag X.Y.Z`
- [ ] Push: `git push && git push --tags`

## Semantic Versioning

Follow [semver](https://semver.org/):

- **MAJOR.x.x** - Breaking changes
- **x.MINOR.x** - New features (backwards compatible)
- **x.x.PATCH** - Bug fixes
