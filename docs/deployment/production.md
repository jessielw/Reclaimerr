# Production

For production deployments, use this guide.

## Required Settings

- Run Reclaimerr behind HTTPS.
- Put the app behind a trusted reverse proxy.
- Persist the data directory across restarts.
- Use a strong `JWT_SECRET` and `ENCRYPTION_KEY`.
- Set `COOKIE_SECURE=true` when the app is only accessed over HTTPS.

## Environment Variables

See the [environment variable reference](../reference/environment-variables.md) for every supported variable, its default, and where it applies. For production, pay particular attention to `DATA_DIR`, `JWT_SECRET`, `ENCRYPTION_KEY`, `COOKIE_SECURE`, proxy trust settings, and `CORS_ORIGINS`.

## Secrets and Persistence

- Store `DATA_DIR` on a persistent volume or disk path.
- Keep `database/reclaimerr.db` with the rest of the app data.
- Do not lose `secrets.env`; it contains generated secrets used to decrypt data.
- Back up the full data directory before upgrading or migrating hosts. See the [backups guide](backups.md).

## Reverse Proxy Checklist

- Forward `X-Forwarded-For` and `X-Forwarded-Proto`.
- Set `PROXY_TRUSTED_HOSTS` to the proxy IP or CIDR. Without it every request appears to come from the proxy, so per-IP limits such as 5 login attempts a minute are shared by all users, and one person mistyping a password can lock everyone else out of local login for a minute.
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
