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

- The **Back Up Database** task runs weekly (Sunday at 1 AM by default, before automatic deletion) and writes `backups/reclaimerr-YYYYMMDD-HHMMSS.db.zip` inside `DATA_DIR`. Each zip holds the database as `reclaimerr.db`, compressed to roughly a fifth of its size.
- It keeps the newest 4 backups. Change this under **Settings → General → Database Backups**.
- **Download backup now** on the same card makes a fresh zipped copy and downloads it. Earlier scheduled backups are listed there for download too.
- Plain `.db` backups made by older versions stay listed, count toward retention, and can still be restored.
- If the backups folder is not writable, the task fails and sends a task failure notification.

These copies contain service API keys, media server tokens, and email settings, so only admins can download them. Keep them private.

By default the `backups/` folder lives inside `DATA_DIR`, on the same disk as the database. To store backups elsewhere, such as a separate drive mounted into the container, set `BACKUP_DIR`:

```yaml
services:
  reclaimerr:
    environment:
      - BACKUP_DIR=/backups
    volumes:
      - ./data:/app/data
      - /mnt/backup-drive/reclaimerr:/backups
```

The folder must be writable by the runtime user. Backups already in the old folder are not moved; copy them over if you want them listed in the app.

### Restoring a Database Backup

#### In the app

1. Under **Settings → General → Database Backups**, click **Restore** next to a stored backup, or **Restore from file** to upload one (for example one from **Download backup now**). Both `.db.zip` and plain `.db` files are accepted.
2. Reclaimerr checks that the file is an intact Reclaimerr database that this version can read, then stages it. Nothing changes yet, and **Cancel restore** discards it.
3. Restart Reclaimerr (for Docker, `docker restart reclaimerr`). On start the staged backup replaces the database, then migrations run.

The database being replaced is kept as `DATA_DIR/database/reclaimerr.db.pre-restore`, so a wrong restore can be undone by hand. Only the most recent one is kept. A backup made by a newer version of Reclaimerr is refused. If the swap fails on start, the current database is left in place and the error is logged.

#### By hand

1. Stop Reclaimerr.
2. Extract `reclaimerr.db` from the backup zip and replace `DATA_DIR/database/reclaimerr.db` with it. Also delete any `reclaimerr.db-wal` and `reclaimerr.db-shm` files next to it, or SQLite will replay them over the restored database. For an older plain `.db` backup, rename it to `reclaimerr.db` instead.
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
