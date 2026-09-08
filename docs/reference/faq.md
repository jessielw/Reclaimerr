# FAQ

## Is automatic deletion enabled by default?

No. Automatic deletion is off by default and requires an explicit opt-in in General Settings before the scheduled task can be enabled.

## Can Reclaimerr delete items without Radarr or Sonarr?

Yes. If the media server supports the action, Reclaimerr can use the server directly. If not, it falls back to local deletion when that setting is enabled.

## Why is an item not being deleted?

The most common reasons are:

- The item is protected.
- A protection request is still pending.
- A delete request is already pending.
- The task is disabled or waiting on the main server.

## What does Leaving Soon do?

Leaving Soon exposes a managed collection of items that are approaching their reclaim deadline. It is a collection view, not poster editing.

Reclaimerr manages exactly two collections per media server, one for movies and one for series. Both names are configurable under **Settings > General > Leaving Soon Collections** and default to `Leaving Soon [Movies]` and `Leaving Soon [Series]`. The two names must differ, because Jellyfin and Emby collections are global and a shared name would resolve to a single collection.

Renaming a collection in Reclaimerr moves it: on the next scan the collection under the old name is deleted and rebuilt under the new one, so any artwork you set on it is lost. Renaming a collection **on the media server** instead is not supported - Reclaimerr finds its collections by name and will simply create a new one.

**Collection Sort** decides the order of items inside the collection. `Server default` leaves whatever ordering the collection already has alone, `Alphabetical` sorts by title, and `Leaving soonest first` puts whatever disappears next at the front. This is **Plex only**: Plex stores a collection's order on the server, while Jellyfin and Emby have no equivalent - a collection there is ordered by whatever each client decides - so the setting is ignored on those servers.

## How do I troubleshoot task failures?

Check the task history first, then confirm the connected media server is reachable and the main server is configured. If you are behind a reverse proxy, verify the forwarded headers and trusted hosts.

## How do I reset the admin password?

Set `ADMIN_PASSWORD` in the environment and restart Reclaimerr. If an admin account already exists, the password for the first admin account is reset on startup. If no admin account exists yet, Reclaimerr creates the initial admin account with that password.

Remove `ADMIN_PASSWORD` after logging in.
