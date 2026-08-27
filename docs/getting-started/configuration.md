# Configuration

Reclaimerr is configured through General Settings, service settings, and a small set of environment variables for the runtime container or desktop process.

## Core Settings Areas

- **Media servers** - connect Plex, Jellyfin, Emby, Radarr, and Sonarr
- **General Settings** - path mappings, move destinations, public application URL, fallback deletion, Leaving Soon, and default auto-delete review periods
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
| `FORWARD_AUTH_ENABLED` | Trust an authenticated username supplied by an approved reverse proxy |
| `FORWARD_AUTH_USER_HEADER` | Forward-auth username header (default: `Remote-User`) |
| `FORWARD_AUTH_TRUSTED_PROXIES` | Direct proxy IPs/CIDRs permitted to supply the identity header |
| `FORWARD_AUTH_ALLOW_LOCAL_FALLBACK` | Recovery switch: allow local login when the asserted username is unrecognized (default: off) |
| `FORWARD_AUTH_LOGOUT_URL` | Identity provider sign-out endpoint used by the UI logout control |
| `JWT_SECRET` | Session signing secret |
| `ENCRYPTION_KEY` | Secrets encryption key |
| `ADMIN_PASSWORD` | Initial admin password or admin password reset on startup |
| `RECLAIMERR_TASK_ISOLATION` | Set to `off` to run heavy tasks inline instead of isolated child processes |
| `RECLAIMERR_COMMAND_WORKERS` | Advanced: internal command executors, from 1 to 8 (default: 2) |

Application URL is configured in General Settings. It is used for Plex and OIDC callback generation behind a reverse proxy.

## Trusted Proxy Authentication

Reclaimerr can trust a username asserted by Authelia or another forward-auth reverse proxy instead of requiring a local sign-in. This is disabled by default, and it never creates accounts or grants roles: it only maps an asserted username onto an existing, active Reclaimerr user. Local sign-in keeps working for requests where the trusted proxy does not assert an identity.

```env
FORWARD_AUTH_ENABLED=true
FORWARD_AUTH_USER_HEADER=Remote-User
FORWARD_AUTH_TRUSTED_PROXIES=172.18.0.4
# Optional: identity provider sign-out endpoint
FORWARD_AUTH_LOGOUT_URL=https://auth.example.com/logout
```

!!! warning "Configure the proxy to strip the header, not just overwrite it"

    Reclaimerr rejects any request that carries two values of the identity
    header, so a proxy that *appends* its own header to a client-supplied one
    already fails safely: the duplicate is refused. The dangerous case is a
    proxy that passes the client's header straight through unchanged on
    requests where the auth step did not set one. Strip the header before the
    auth step runs, not after it, so there is nothing left for the auth step
    to accidentally pass through.

    Caddy example using `forward_auth` with Authelia:

    ```caddy
    reclaimerr.example.com {
        route {
            # Strip any client-supplied identity header before authentication runs,
            # so it can never survive into the upstream request.
            request_header -Remote-User

            forward_auth authelia:9091 {
                uri /api/authz/forward-auth
                copy_headers Remote-User
            }
        }

        reverse_proxy reclaimerr:8000
    }
    ```

    The `route` block matters here: Caddy's built-in directive order runs
    `forward_auth` before `request_header` by default, regardless of the
    order they are written. Without `route`, the header would not actually be
    stripped before the auth step runs.

    `Remote-User` here is the default. If `FORWARD_AUTH_USER_HEADER` is set to
    something else, strip that header name instead of `Remote-User`.

Create the Reclaimerr user first, then enable the feature. The asserted username must match an existing Reclaimerr username exactly, including case. A mismatch produces a 401 on every request, plus a log line naming the asserted username.

You do not need a second account to line the two names up. An administrator can rename any existing user, including the initial `admin`, from **Settings -> Users** by editing the user and changing the Username field. Renaming does not sign the user out and keeps their reclaim history, since accounts are tracked by ID internally. If the renamed user signs in through the proxy, update the proxy to assert the new username as well.

If you get locked out, for example the feature was enabled before the matching user existed, set `FORWARD_AUTH_ALLOW_LOCAL_FALLBACK=true` and restart. Sign in locally, create the matching user, then remove the variable and restart again. While the variable is set, an admin notice stays visible in the sidebar as a reminder to turn it back off.

`FORWARD_AUTH_TRUSTED_PROXIES` takes a comma-separated list of direct proxy IPs and CIDRs: the address of the reverse proxy container or host itself, not the client behind it. It rejects both `*` and all-address ranges such as `0.0.0.0/0` or `::/0`.

For a trusted-proxy session, the sidebar logout control only appears when `FORWARD_AUTH_LOGOUT_URL` is set, since signing out of the identity provider is the proxy's job, not Reclaimerr's. When it is unset, the logout control is replaced by a "Managed by SSO" label. This does not apply to a local session started through `FORWARD_AUTH_ALLOW_LOCAL_FALLBACK` recovery mode: that session gets the normal logout button regardless of `FORWARD_AUTH_LOGOUT_URL`.

## Multi-Server Setup

- Pick exactly one media server as the main server.
- Keep all connected servers pointed at the same physical media library.
- Use path mappings if the media server paths do not match local paths.

The main server is the only source of library and file-version rows. Every other configured server is linked: it contributes watch state and same-media matches, but never library contents.

Each media server card in **Settings -> Media Servers** has its own **Sync** button and reports when that server was last synced. Syncing the main server runs the full media sync (libraries, movies, series, then every linked server). Syncing a linked server refreshes only that server's watch data and supplemental matches, so it neither waits on nor blocks the others. The dashboard shows each server's own last-sync time, labelled main or linked.

## Disabling Or Deleting Offline Services

Service configuration changes do not require the external service to be online. An existing Radarr, Sonarr, Seerr, Tautulli, Tracearr, Jellyfin, Emby, or Plex configuration can be disabled or deleted while that service is unreachable.

## Tracearr Playback History

Tracearr requires its stable public API v2, available in Tracearr 2.0.0 and newer. Add the Tracearr base URL and a public API key, choose **Discover servers**, then confirm the Tracearr server that belongs to each configured Plex, Jellyfin, or Emby server. One Tracearr instance can cover multiple media servers.

A binding selects the single durable history provider for that media server: Tracearr replaces Tautulli for a bound Plex server and Playback Reporting for a bound Jellyfin or Emby server. Reclaimerr never combines those durable event streams or silently falls back when the selected Tracearr source is offline. Jellyfin and Emby native current-watch snapshots remain complementary and are still used where applicable. Remove a binding to return to the prior durable provider; retained events are remapped without double-counting.

- **Disable** keeps the saved URL, credentials, and related configuration for later use.
- **Delete** permanently removes the service configuration without contacting the external service.
- Enabling a service or saving a new enabled configuration still requires a successful connection test.
- The active main media server cannot be disabled or deleted. Assign another configured media server as main first.

Deleting a Radarr or Sonarr instance also performs local dependency cleanup:

- stored media references for that instance are removed;
- the instance is removed from explicitly assigned rules; a rule is disabled only when that removal leaves it with no selected ARR instances;
- path mappings scoped only to that instance are removed.

The Settings page reports when dependent rules or path mappings were changed. Review any disabled rules before enabling them again and select the intended ARR instances.

## Safety Settings Worth Reviewing

- `Allow Media Server Fallback Deletion`
- `Default ARR Delete Behavior`
- `Add Arr Import List Exclusions on Delete`
- `Default Auto-Delete Review Periods`
- `Move Destination Folders`

## Resetting The Admin Password

Set `ADMIN_PASSWORD` in the environment, start Reclaimerr, sign in with the new password, then remove `ADMIN_PASSWORD` again.

If an admin account already exists, Reclaimerr resets that account's password on startup. If no admin account exists yet, Reclaimerr creates the initial admin account with that password.
