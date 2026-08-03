# Configuration

Reclaimerr is configured through General Settings, service settings, and a small
set of environment variables for the runtime container or desktop process.

## Core Settings Areas

- **Media servers** - connect Plex, Jellyfin, Emby, Radarr, and Sonarr
- **General Settings** - path mappings, move destinations, public application
  URL, fallback deletion, Leaving Soon, and default auto-delete review periods
- **Tasks** - schedule scans, tagging, syncs, and optional auto-deletion
- **Notifications** - configure Apprise destinations

## Important Environment Variables

| Variable                     | Purpose                                                                    |
| ---------------------------- | -------------------------------------------------------------------------- |
| `API_HOST`                   | Bind address for the API server                                            |
| `API_PORT`                   | HTTP port for the API server                                               |
| `DATA_DIR`                   | Persistent application data location                                       |
| `TZ`                         | Local timezone for cron-style schedules                                    |
| `UMASK`                      | Default permissions for created files                                      |
| `PROXY_TRUSTED_HOSTS`        | Trusted reverse proxy IPs or CIDRs                                         |
| `FORWARD_AUTH_ENABLED`       | Trust an authenticated username supplied by an approved reverse proxy      |
| `FORWARD_AUTH_USER_HEADER`   | Forward-auth username header (default: `Remote-User`)                      |
| `FORWARD_AUTH_TRUSTED_PROXIES` | Direct proxy IPs/CIDRs permitted to supply the identity header            |
| `JWT_SECRET`                 | Session signing secret                                                     |
| `ENCRYPTION_KEY`             | Secrets encryption key                                                     |
| `ADMIN_PASSWORD`             | Initial admin password or admin password reset on startup                  |
| `RECLAIMERR_TASK_ISOLATION`  | Set to `off` to run heavy tasks inline instead of isolated child processes |
| `RECLAIMERR_COMMAND_WORKERS` | Advanced: internal command executors, from 1 to 8 (default: 2)             |

Application URL is configured in General Settings. It is used for Plex and OIDC
callback generation behind a reverse proxy.

## Trusted Proxy Authentication

Reclaimerr can use an identity asserted by Authelia or another forward-auth
proxy. This is disabled by default and maps only to an existing, active
Reclaimerr user; it never creates accounts or grants roles automatically.

```env
FORWARD_AUTH_ENABLED=true
FORWARD_AUTH_USER_HEADER=Remote-User
FORWARD_AUTH_TRUSTED_PROXIES=172.18.0.4
```

The supplied username must exactly match a Reclaimerr username. Configure the
allowlist with the direct TCP address or CIDR of the reverse proxy container or
host. Wildcards are rejected. Keep Reclaimerr's backend port inaccessible to
untrusted clients, and configure the proxy to overwrite rather than preserve
any incoming identity header.

Local cookie login remains available when the trusted proxy does not assert an
identity. When it does assert one, that identity is authoritative and an
unknown or disabled user is denied instead of falling back to a local session.
Signing out of the upstream identity provider remains the proxy's responsibility.

## Multi-Server Setup

- Pick exactly one media server as the main server.
- Keep all connected servers pointed at the same physical media library.
- Use path mappings if the media server paths do not match local paths.

## Disabling Or Deleting Offline Services

Service configuration changes do not require the external service to be online.
An existing Radarr, Sonarr, Seerr, Tautulli, Jellyfin, Emby, or Plex
configuration can be disabled or deleted while that service is unreachable.

- **Disable** keeps the saved URL, credentials, and related configuration for
  later use.
- **Delete** permanently removes the service configuration without contacting
  the external service.
- Enabling a service or saving a new enabled configuration still requires a
  successful connection test.
- The active main media server cannot be disabled or deleted. Assign another
  configured media server as main first.

Deleting a Radarr or Sonarr instance also performs local dependency cleanup:

- stored media references for that instance are removed;
- rules explicitly assigned to the instance are disabled and their instance
  selection is cleared;
- path mappings scoped only to that instance are removed.

The Settings page reports when dependent rules or path mappings were changed.
Review disabled rules before enabling them again and select the intended ARR
instance.

## Safety Settings Worth Reviewing

- `Allow Media Server Fallback Deletion`
- `Default ARR Delete Behavior`
- `Add Arr Import List Exclusions on Delete`
- `Default Auto-Delete Review Periods`
- `Move Destination Folders`

## Resetting The Admin Password

Set `ADMIN_PASSWORD` in the environment, start Reclaimerr, sign in with the new
password, then remove `ADMIN_PASSWORD` again.

If an admin account already exists, Reclaimerr resets that account's password
on startup. If no admin account exists yet, Reclaimerr creates the initial admin
account with that password.
