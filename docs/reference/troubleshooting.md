# Troubleshooting

## Rules Not Working As Expected

- Be sure you have all your services configured.
- Be sure you have ran a full scan (especially after making changes to any services).

### Seerr Requester Watch Rules Do Not Match

- Requester mappings live under **Settings -> User Signals -> Seerr Requester to Watch User Mapping**. The **Sign-In Identity Links** panel under **Settings -> Users** links media-server logins to local accounts and has no effect on rules.
- Confirm the playback happened after the movie or relevant TV season was requested. Earlier playback intentionally does not count for `Seerr requester watched after requesting`; use `Seerr requester has watched` in rules that should not care about dates.
- If every item reports plays from before its request, check the request dates themselves. A rebuilt or migrated Seerr, or a re-requested season, dates its rows after the plays they describe, which makes the date comparison un-passable for a whole library. **Settings -> User Signals -> Ignore Seerr Request Dates** turns that comparison off for every rule at once.
- For season and series targets, one requester must have watched every required local episode; progress from multiple requesters is not combined.
- Check that the season was actually included in the Seerr request. An un-requested season does not inherit another season's state.
- Automatic matching uses the Seerr username, display name, email, and linked Plex or Jellyfin account name, plus every name the playback provider reports for that account. Add an explicit requester mapping only when the two share no name at all.
- Partial playback does not count, regardless of session length. The media server's watched state or Tautulli's completed status must confirm completion.
- Plex durable history requires Tautulli or a Plex-bound Tracearr server. Tautulli and Tracearr usernames are matched as Plex identities. Binding Tracearr to a Plex server retires Tautulli for playback totals, but completed Tautulli plays still count as watch evidence.
- Declined and failed Seerr requests are ignored.

## The UI Does Not Load

- Confirm the backend is running.
- Confirm the frontend dev server is running if you are in source mode.
- Check that the configured API port is reachable.

## Scheduled Tasks Do Not Run

- Verify the task is enabled in Tasks.
- Verify the task is not waiting on a main media server.
- Check the task status and recent run history in the UI.

## Reverse Proxy Problems

- Make sure `X-Forwarded-Proto` reaches the app.
- Set `PROXY_TRUSTED_HOSTS` to the proxy IP or CIDR.
- Recheck `Application URL` in General Settings. Use `redirect_uri_override` only when OIDC needs a different callback.

## Deletion Is Skipped

- Protected media is skipped by design.
- Pending protection requests block automatic deletion.
- Pending delete requests also block automatic deletion.

## A Manual Deletion Reports "Failed" With No Reason

Deleting from the Candidates page ends in `Manual deletion complete: 0 processed, 1 failed` in the log, and the toast says the item could not be deleted. The reason is on the line just above it, at `WARNING` or `ERROR`, and it is also stored on the candidate and repeated in the toast. The usual causes:

- **`No delete route available`** - no Radarr/Sonarr instance and no main media server are initialized, so there is nowhere to send the delete. This is normally a service that failed to start up rather than one that is misconfigured now: search the log from the last boot for `service initialization failed`. Restarting Reclaimerr re-runs the connection, and the Test button in Settings does not, so a passing Test does not prove the running instance has a usable client.
- **`Stale candidate: ... is tombstoned in Reclaimerr`** - the movie or series behind the candidate is marked removed, so no handler will act on it. Run a library sync followed by a reclaim scan to clear the leftover candidate.
- **`Stale candidate: ... no longer exists`** - the candidate points at a media row that is gone. A reclaim scan rebuilds candidates and clears it.

## A Configured Service Is Offline

You can disable or delete an existing service configuration even when the external service is unreachable. The Test action and enabling a service still require connectivity.

The active main media server is the exception: assign another media server as main before disabling or deleting it.

When deleting a Radarr or Sonarr instance, Reclaimerr removes that target from assigned rules and removes path mappings scoped specifically to it. A rule is disabled only when no selected ARR targets remain. Review the warning shown after deletion and update disabled rules before re-enabling them.
