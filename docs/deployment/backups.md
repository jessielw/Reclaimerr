# Backups

Back up the full `DATA_DIR`. That directory contains the app state you need to recover a Reclaimerr instance.

## Backup Contents

- `database/reclaimerr.db`
- `secrets.env`
- `logs/` if you need local audit history
- any custom static assets stored under the app data tree

## Why Backups Matter

- The database stores settings, schedules, candidates, requests, and history.
- `secrets.env` contains the generated secrets used to decrypt sensitive data.
- Without the full data directory, a restore may be incomplete or inconsistent.

## Built-in Database Backups

Reclaimerr can back up its own database while it runs, so you do not need to stop it first.

- The **Back Up Database** task runs weekly (Sunday at 1 AM by default, before automatic deletion) and writes `backups/reclaimerr-YYYYMMDD-HHMMSS.db` inside `DATA_DIR`.
- It keeps the newest 4 backups. Change this under **Settings → General → Database Backups**.
- **Download backup now** on the same card makes a fresh copy and downloads it. Earlier scheduled backups are listed there for download too.
- If the backups folder is not writable, the task fails and sends a task failure notification.

These copies contain service API keys, media server tokens, and email settings, so only admins can download them. Keep them private.

The `backups/` folder lives inside `DATA_DIR`, on the same disk as the database. Copy it somewhere else as well.

### Restoring a Database Backup

1. Stop Reclaimerr.
2. Replace `DATA_DIR/database/reclaimerr.db` with the backup file, renamed to `reclaimerr.db`.
3. Start Reclaimerr. Migrations run on start, so a backup from an older version upgrades itself.

Restoring onto a new install also needs the original `secrets.env`. Without it, the saved API keys and tokens cannot be decrypted and must be entered again.

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
