# Deletion Flow

Reclaimerr deletes candidates in a fixed order.

Scheduled automatic deletion only applies to candidates from rules that
explicitly enable automatic deletion. It then applies the candidate's review
period. The default movie and TV delays can be overridden by auto-delete-enabled
candidate rules, and the longest delay from all matching opted-in rules wins.
Candidates remain visible and tagged while waiting. Manual delete and move
actions are immediate and do not use this delay.

Cleanup-candidate rules can also enable **Move Instead of Delete**. When a
delete action runs for one of those candidates, Reclaimerr moves the file or
folder to the configured destination and removes the source record instead of
deleting the file. If multiple matched rules disagree, move wins over delete.
Item-scoped movie folders move as a unit, including artwork, trailers, and all
sidecars. Shared folders stay conservative: Reclaimerr moves only the selected
media plus its matching sidecars (including language-tagged subtitles such as
`.en.srt`) and removes the source folder only when it is empty.
Existing destination folders are merged safely. When the same relative file is
already present, Reclaimerr verifies matching size and SHA-256 content before
discarding the redundant source copy; different files are never overwritten and
leave the candidate available for review.

When Leaving Soon collections are enabled, Reclaimerr first removes the affected
movie or series from its managed Plex, Jellyfin, and Emby collections. This
prevents media-server collections from retaining links to files that are about
to disappear. If an affected collection cannot be updated, the delete or move is
blocked and retried later. After the operation, Reclaimerr reconciles the
collections so failed or partially completed actions remain represented.

## Deletion Modes

| Mode                | Behavior                                                                        |
| ------------------- | ------------------------------------------------------------------------------- |
| `delete`            | Delete through Radarr, Sonarr, or the media server route used for the candidate |
| `move`              | Move to the configured destination and remove the source record                 |
| `fallback deletion` | Use the media server when ARR cannot handle the delete path                     |

## Routing Order

1. Use Radarr for movie candidates when the candidate is linked to Radarr and
   the action can be handled there.
2. Use Sonarr for series, season, and episode candidates when the candidate is
   linked to Sonarr and the action can be handled there.
3. Fall back to the main media server when the ARR service cannot handle the
   deletion or no ARR route exists.
4. Use local deletion only when that fallback is enabled and the candidate can
   be removed locally.

## What The Fallback Covers

- movie candidates that Radarr cannot remove directly
- series candidates that Sonarr cannot remove directly
- scoped deletions that need the media server to remove files or folders

## Important Settings

- `Default ARR Delete Behavior`
- `Allow Media Server Fallback Deletion`
- `Add Arr Import List Exclusions on Delete`
- `Move Destination Folders`

## Related Pages

- [How It Works](how-it-works.md)
- [Tasks](tasks.md)
- [Production](../deployment/production.md)
