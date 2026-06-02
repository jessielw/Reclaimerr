# Backups

Back up the full `DATA_DIR`. That directory contains the app state you need to
recover a Reclaimerr instance.

## Backup Contents

- `database/reclaimerr.db`
- `secrets.env`
- `logs/` if you need local audit history
- any custom static assets stored under the app data tree

## Why Backups Matter

- The database stores settings, schedules, candidates, requests, and history.
- `secrets.env` contains the generated secrets used to decrypt sensitive data.
- Without the full data directory, a restore may be incomplete or inconsistent.

## Backup Routine

- Stop the app or take a filesystem snapshot before copying the database.
- Copy the entire data directory to a separate location.
- Keep at least one off-host copy.
- Test a restore on a non-production instance before relying on the process.

## Restore Steps

1. Stop Reclaimerr.
2. Restore the saved `DATA_DIR` contents.
3. Make sure ownership and permissions match the runtime user.
4. Start the app and verify `/api/version` and the UI load correctly.
5. Check settings, tasks, and history for expected state.

## Related Pages

- [Production](production.md)
- [Docker](docker.md)

