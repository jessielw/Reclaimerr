# Configuration

Reclaimerr is configured through General Settings, service settings, and a small
set of environment variables for the runtime container or desktop process.

## Core Settings Areas

- **Media servers** - connect Plex, Jellyfin, Emby, Radarr, and Sonarr
- **General Settings** - path mappings, public application URL, fallback
  deletion, Leaving Soon, and automatic cleanup deletion
- **Tasks** - schedule scans, tagging, syncs, and optional auto-deletion
- **Notifications** - configure Apprise destinations

## Important Environment Variables

| Variable | Purpose |
| --- | --- |
| `API_HOST` | Bind address for the API server |
| `API_PORT` | HTTP port for the API server |
| `DATA_DIR` | Persistent application data location |
| `TZ` | Local timezone for cron-style schedules |
| `UMASK` | Default permissions for created files |
| `PROXY_TRUSTED_HOSTS` | Trusted reverse proxy IPs or CIDRs |
| `JWT_SECRET` | Session signing secret |
| `ENCRYPTION_KEY` | Secrets encryption key |
| `ADMIN_PASSWORD` | Initial admin password or admin password reset on startup |

Application URL is configured in General Settings. It is used for Plex and OIDC
callback generation behind a reverse proxy.

## Multi-Server Setup

- Pick exactly one media server as the main server.
- Keep all connected servers pointed at the same physical media library.
- Use path mappings if the media server paths do not match local paths.

## Safety Settings Worth Reviewing

- `Allow Media Server Fallback Deletion`
- `Default ARR Delete Behavior`
- `Add Arr Import List Exclusions on Delete`
- `Enable Automatic Cleanup Deletion`

## Resetting The Admin Password

Set `ADMIN_PASSWORD` in the environment, start Reclaimerr, sign in with the new
password, then remove `ADMIN_PASSWORD` again.

If an admin account already exists, Reclaimerr resets that account's password
on startup. If no admin account exists yet, Reclaimerr creates the initial admin
account with that password.

