# Features

Reclaimerr is built around a predictable reclaim pipeline: scan, review, protect, approve, then delete or move through the correct service.

## Core Workflow

- Sync media and metadata from Plex, Jellyfin, and Emby
- Scan for reclaim candidates using your configured rules
- Track candidate scopes at the item, series, season, and episode level
- Respect protection, pending requests, and deletion history
- Route deletions through the media server, Radarr, or Sonarr when possible

## Operational Features

| Feature | What It Gives You |
| --- | --- |
| Leaving Soon | Named, auto-synced collections for items that are approaching removal, with optional custom poster art, optionally ordered by deadline on Plex |
| Duplicates | Movies and episodes with more than one file, a suggested keeper, and one-click removal of the other copies |
| Calendar | Month and week views of when flagged media is due to be deleted or moved, with per-day counts and reclaimable space |
| Storage | Disk capacity per mount and per Arr instance, library size, reclaimable space, and reclaimed totals by outcome |
| Logs | Read, filter, follow, and download the application log from Settings |
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
- Seerr
- Tautulli
- Tracearr 2.0.0 or newer
