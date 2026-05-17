# Alembic Notes

- The runtime always upgrades to `head`, so the repo must resolve to a single Alembic head.
- After merging feature branches that each added migrations, create an Alembic merge revision before shipping if `alembic heads` would show more than one head.
- Keep each migration filename aligned with its declared `revision` for easier debugging.
- Prefer making new migrations from the current repo head, not from an older branch local revision.
