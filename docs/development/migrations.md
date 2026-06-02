# Database Migrations

Reclaimerr uses Alembic for schema migrations. They run automatically on startup,
so users do not need to manage them manually.

## If You Change A Model

Generate a migration after changing `backend/database/models.py`:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Review the generated file before committing it.

## Keep The Repo At One Head

- The repository should resolve to a single Alembic head.
- If two feature branches add migrations, create a merge revision before
  shipping.
- Keep migration filenames aligned with their declared revision IDs.

