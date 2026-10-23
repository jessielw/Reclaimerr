# Environment Variables

This page is the reference for environment variables read by Reclaimerr, its Docker image, desktop launcher, and included maintenance scripts. App settings can be supplied in the process environment or the project `.env` file. Backend settings also load `DATA_DIR/secrets.env` first; process environment values take precedence over files.

## Backend Settings

| Variable | Default | How it is used |
| --- | --- | --- |
| `DATA_DIR` | `./data` | Root for the database, logs, cache, static files, generated secrets, and default backups. Keep it persistent. |
| `BACKUP_DIR` | `<DATA_DIR>/backups` | Overrides the database backup destination. |
| `STATIC_DIR` | `./data/static` | Root for files served by the app. |
| `AVATARS_DIR` | `./data/static/avatars` | User avatar storage directory. |
| `COLLECTION_POSTERS_DIR` | `./data/static/collection-posters` | Custom media server collection poster storage. |
| `FRONTEND_DIST` | unset | Built frontend directory. When set, the backend serves the UI. Docker and desktop launchers set this automatically. |
| `API_HOST` | `0.0.0.0` | Address the API server binds to. |
| `API_PORT` | `8000` | API listening port. |
| `CORS_ORIGINS` | `*` | Comma-separated browser origins allowed by the API. Restrict this for deployments with a separate UI origin. |
| `PROXY_TRUSTED_HOSTS` | `127.0.0.1,::1` | Proxy IPs/CIDRs allowed to supply `X-Forwarded-*` headers. This controls client IP and scheme handling. |
| `COOKIE_SECURE` | `false` | Marks authentication cookies `Secure`; enable when serving over HTTPS. |
| `AUTH_FORCE_LOCAL_LOGIN` | `false` | Temporary recovery override that enables local login regardless of saved login method settings. It does not bypass trusted-proxy checks. Remove after recovery. |
| `FORWARD_AUTH_ENABLED` | `false` | Enables login using a username asserted by a trusted reverse proxy. Requires `FORWARD_AUTH_TRUSTED_PROXIES`. |
| `FORWARD_AUTH_USER_HEADER` | `Remote-User` | HTTP header containing the proxy-authenticated username. |
| `FORWARD_AUTH_TRUSTED_PROXIES` | empty | Comma-separated direct proxy IPs/CIDRs permitted to assert identity. Required when forward auth is enabled; `*` and all-address ranges are rejected. |
| `FORWARD_AUTH_ALLOW_LOCAL_FALLBACK` | `false` | Temporary recovery switch: allow local login if the proxy asserts a username that does not exist in Reclaimerr. Remove after recovery. |
| `FORWARD_AUTH_LOGOUT_URL` | empty | Absolute HTTP(S) identity-provider logout URL shown for proxy-authenticated sessions. |
| `ADMIN_PASSWORD` | unset | Creates the initial admin with this password or resets the first admin at startup. Remove it after use. |
| `JWT_SECRET` | generated | Signs session tokens. Generated values are saved in `<DATA_DIR>/secrets.env`; provide a stable value only if managing secrets yourself. Explicit values must be at least 32 characters. |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. Normally leave at the default. |
| `ENCRYPTION_KEY` | generated | Encrypts sensitive database fields. Generated values are saved in `<DATA_DIR>/secrets.env`; explicit values must be at least 32 characters. Back it up with the data directory. |
| `TMDB_API_KEY` | unset | Optional TMDB read access token override for metadata lookups; the app has a built-in fallback. |
| `LOG_LEVEL` | `INFO` | Minimum runtime log severity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. Read at startup. |
| `LOG_RETENTION_DAYS` | `30` | Number of days to keep rotated log files; values below 1 are raised to 1. |
| `RECLAIMERR_COMMAND_WORKERS` | `2` | Number of internal durable command executors. Supported range is 1-8. |
| `RECLAIMERR_TASK_ISOLATION` | `auto` | Set to `off` to run eligible heavy tasks in the API process instead of isolated child processes. |

The generated `JWT_SECRET` and `ENCRYPTION_KEY` are written to `DATA_DIR/secrets.env` on first startup. Preserve that file when moving or restoring an instance or existing sessions and encrypted settings may stop working.

## Docker Image Variables

These are consumed by the Docker entrypoint before the backend starts.

| Variable | Default | How it is used |
| --- | --- | --- |
| `PUID` | image default user | Optional numeric UID for the app process and ownership of `/app/data`. Set it together with `PGID`. |
| `PGID` | image default group | Optional numeric GID for the app process and ownership of `/app/data`. Set it together with `PUID`. |
| `UMASK` | system default | Optional 1-4 digit octal umask applied to files created by the process, for example `022`. |
| `TZ` | image timezone | Optional timezone name, such as `America/New_York` or `UTC`; sets the container timezone used by schedules and local timestamps. |
| `APP_USER` | `reclaimerr` | Name used when the entrypoint creates/remaps the app account for `PUID`/`PGID`. Usually leave unchanged. |
| `APP_GROUP` | `reclaimerr` | Name used when the entrypoint creates/remaps the app group for `PUID`/`PGID`. Usually leave unchanged. |
| `APP_HOME` | `/app/data` | Home directory assigned if the entrypoint creates the app account. |

The image also sets `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`, and `PIP_NO_CACHE_DIR=1` for Python bytecode, output buffering, and package installation behavior. These are image defaults, not Reclaimerr settings.

## Build-Time and Desktop Variables

| Variable | Scope | How it is used |
| --- | --- | --- |
| `VITE_APP_CHANNEL` | Frontend build | Embeds the app channel (`dev` by default) into the built UI. Set during the Docker or desktop frontend build; changing the runtime container environment does not change an existing build. |
| `CORS_ORIGIN` | Bundled desktop `.env` | Desktop launcher compatibility setting for its single UI origin. It maps this value to the backend's `CORS_ORIGINS`. |

The desktop launcher also reads `API_HOST`, `API_PORT`, and `ADMIN_PASSWORD` from a bundled `.env`. It sets its data/static/frontend paths and API CORS origin itself, and generates persistent secrets in its user data directory.

## Internal Process Variable

| Variable | Purpose |
| --- | --- |
| `RECLAIMERR_TASK_CHILD` | Internal marker set to `1` only for an isolated task child process. It changes child logging and prevents that process from spawning another child. Do not set it for the main app. |

## Included Script Variables

The examples in [Scripting ARR Changes](../usage/scripting-arr-changes.md) use these variables for the maintenance script, not for Reclaimerr's server configuration:

| Variable | Purpose |
| --- | --- |
| `RECLAIMERR` | Reclaimerr base URL. |
| `RECLAIMERR_TOKEN` | Reclaimerr API bearer token. |
| `RADARR` | Radarr base URL. |
| `RADARR_API_KEY` | Radarr API key. |
| `SONARR` | Sonarr base URL. |
| `SONARR_API_KEY` | Sonarr API key. |
| `DRY_RUN` | In the Sonarr example, set to `1` to print planned changes without applying them. |

The separate `radarr_reprofile_matches.py` script uses `RECLAIMERR`, `RECLAIMERR_TOKEN`, `RADARR`, and `RADARR_API_KEY`.
