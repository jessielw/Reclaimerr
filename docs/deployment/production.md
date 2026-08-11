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
| `FORWARD_AUTH_ENABLED` | Enables opt-in trusted-header authentication |
| `FORWARD_AUTH_USER_HEADER` | Selects the proxy-provided username header |
| `FORWARD_AUTH_TRUSTED_PROXIES` | Restricts identity headers to direct proxy IPs/CIDRs |
| `FORWARD_AUTH_ALLOW_LOCAL_FALLBACK` | Recovery switch for an unrecognised proxy identity; remove after use |
| `FORWARD_AUTH_LOGOUT_URL` | Identity provider sign-out endpoint used by the UI logout control |
| `CORS_ORIGINS` | Restricts the UI origins that can talk to the API |
| `COOKIE_SECURE` | Marks auth cookies secure when served over HTTPS |
| `RECLAIMERR_COMMAND_WORKERS` | Advanced internal command executor count (default `2`, range `1`-`8`) |

## Secrets and Persistence

- Store `DATA_DIR` on a persistent volume or disk path.
- Keep `database/reclaimerr.db` with the rest of the app data.
- Do not lose `secrets.env`; it contains generated secrets used to decrypt data.
- Back up the full data directory before upgrading or migrating hosts. See the [backups guide](backups.md).

## Reverse Proxy Checklist

- Forward `X-Forwarded-For` and `X-Forwarded-Proto`.
- Set `PROXY_TRUSTED_HOSTS` to the proxy IP or CIDR.
- Verify the public callback URLs for OIDC or Plex login flows. Use `Application URL` in General Settings for the shared public base URL, and keep `redirect_uri_override` for OIDC-only cases.
- Keep the backend port private if the proxy is the only ingress point.
- When using trusted-header authentication, never use a wildcard or all-address proxy range such as `0.0.0.0/0`; Reclaimerr rejects both. The proxy must strip the client-supplied identity header before the auth step runs, not just overwrite it. See [Trusted Proxy Authentication](../getting-started/configuration.md#trusted-proxy-authentication) for the reasoning and a verified Caddy example.
- Reclaimerr has no CSRF tokens and relies on `SameSite=lax` session cookies. Under trusted-header authentication its own cookie is not what authenticates the request, so cross-site protection depends on your proxy's session cookie configuration. Keep that cookie's `SameSite` at `lax` or stricter.

## Operational Notes

- Run exactly one Reclaimerr process or replica against a SQLite database. The in-process workflow locks and command claim coordinator do not coordinate multiple application processes. Reclaimerr handles concurrency with its internal command executors instead.
- Keep `DATA_DIR` on local storage. SQLite is not supported on NFS or other network filesystems; use host-local storage with a persistent volume mapping.
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
