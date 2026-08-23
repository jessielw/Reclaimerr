# Troubleshooting

## Rules Not Working As Expected

- Be sure you have all your services configured.
- Be sure you have ran a full scan (especially after making changes to any services).

### Seerr Requester Watch Rules Do Not Match

- Requester mappings live under **Settings -> User Signals -> Seerr Requester to Watch User Mapping**. The **Sign-In Identity Links** panel under **Settings -> Users** links media-server logins to local accounts and has no effect on rules.
- Confirm the playback happened after the movie or relevant TV season was requested. Earlier playback intentionally does not count.
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

## A Configured Service Is Offline

You can disable or delete an existing service configuration even when the external service is unreachable. The Test action and enabling a service still require connectivity.

The active main media server is the exception: assign another media server as main before disabling or deleting it.

When deleting a Radarr or Sonarr instance, Reclaimerr removes that target from assigned rules and removes path mappings scoped specifically to it. A rule is disabled only when no selected ARR targets remain. Review the warning shown after deletion and update disabled rules before re-enabling them.
