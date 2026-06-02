# Features

Reclaimerr is built around a predictable reclaim pipeline: scan, review,
protect, approve, then delete or move through the correct service.

## Core Workflow

- Sync media and metadata from Plex, Jellyfin, and Emby
- Scan for reclaim candidates using your configured rules
- Track candidate scopes at the item, series, season, and episode level
- Respect protection, pending requests, and deletion history
- Route deletions through the media server, Radarr, or Sonarr when possible

## Operational Features

| Feature | What It Gives You |
| --- | --- |
| Leaving Soon | A visible collection for items that are approaching removal |
| Scheduled tasks | Automated sync, scanning, and optional deletion workflows |
| Protection flow | Keep items out of deletion while a request is pending or approved |
| Reclaim history | Audit what happened, when it happened, and who approved it |
| Fallback deletion | Delete locally when a media server cannot handle the action |

## Safety Controls

- Automatic cleanup deletion is opt-in.
- Protected media is skipped by design.
- Pending protection requests and pending delete requests block deletion.
- Main-server-dependent tasks stay disabled until the main server is available.

## Supported Services

- Plex
- Jellyfin
- Emby
- Radarr
- Sonarr

