# Production

For production deployments, use this guide.

## Required Settings

- Run Reclaimerr behind HTTPS.
- Put the app behind a trusted reverse proxy.
- Persist the data directory across restarts.
- Use a strong `JWT_SECRET` and `ENCRYPTION_KEY`.
- Set `COOKIE_SECURE=true` when the app is only accessed over HTTPS.

## Environment Variables

| Variable | Why It Matters |
| --- | --- |
| `DATA_DIR` | Keeps the database, logs, cache, and generated secrets in one persistent location |
| `API_HOST` | Bind address for the backend process |
| `API_PORT` | Port exposed by the backend process |
| `TZ` | Keeps cron schedules and timestamps aligned with your locale |
| `PROXY_TRUSTED_HOSTS` | Ensures forwarded headers are only accepted from trusted proxies |
| `CORS_ORIGINS` | Restricts the UI origins that can talk to the API |
| `COOKIE_SECURE` | Marks auth cookies secure when served over HTTPS |

## Secrets and Persistence

- Store `DATA_DIR` on a persistent volume or disk path.
- Keep `database/reclaimerr.db` with the rest of the app data.
- Do not lose `secrets.env`; it contains generated secrets used to decrypt data.
- Back up the full data directory before upgrading or migrating hosts. See the
  [backups guide](backups.md).

## Reverse Proxy Checklist

- Forward `X-Forwarded-For` and `X-Forwarded-Proto`.
- Set `PROXY_TRUSTED_HOSTS` to the proxy IP or CIDR.
- Verify the public callback URLs for OIDC or Plex login flows. Use
  `Application URL` in General Settings for the shared public base URL, and
  keep `redirect_uri_override` for OIDC-only cases.
- Keep the backend port private if the proxy is the only ingress point.

## Operational Notes

- Keep the main media server configured if you use tasks that require it.
- Review scheduled tasks before enabling automatic deletion.
- Check task history and background jobs during upgrades or troubleshooting.
- Use the docs site and API reference to confirm endpoint behavior after updates.

## Upgrade Checklist

1. Stop the app.
2. Back up `DATA_DIR`.
3. Pull or install the new release.
4. Run database migrations if your deployment process requires them.
5. Start the app and check `/api/version`.
6. Confirm the UI loads and tasks can be scheduled.
