# FAQ

## Is automatic deletion enabled by default?

No. Automatic deletion is off by default and requires an explicit opt-in in
General Settings before the scheduled task can be enabled.

## Can Reclaimerr delete items without Radarr or Sonarr?

Yes. If the media server supports the action, Reclaimerr can use the server
directly. If not, it falls back to local deletion when that setting is enabled.

## Why is an item not being deleted?

The most common reasons are:

- The item is protected.
- A protection request is still pending.
- A delete request is already pending.
- The task is disabled or waiting on the main server.

## What does Leaving Soon do?

Leaving Soon exposes a managed collection of items that are approaching their
reclaim deadline. It is a collection view, not poster editing.

## How do I troubleshoot task failures?

Check the task history first, then confirm the connected media server is
reachable and the main server is configured. If you are behind a reverse proxy,
verify the forwarded headers and trusted hosts.

## How do I reset the admin password?

Set `ADMIN_PASSWORD` in the environment and restart Reclaimerr. If an admin
account already exists, the password for the first admin account is reset on
startup. If no admin account exists yet, Reclaimerr creates the initial admin
account with that password.

Remove `ADMIN_PASSWORD` after logging in.

