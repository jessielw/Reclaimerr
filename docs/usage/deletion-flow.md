# Deletion Flow

Reclaimerr deletes candidates in a fixed order.

Scheduled automatic deletion only applies to candidates from rules that explicitly enable automatic deletion. It then applies the candidate's review period. The default movie and TV delays can be overridden by auto-delete-enabled candidate rules, and the longest delay from all matching opted-in rules wins. Candidates remain visible and tagged while waiting. Manual delete and move actions are immediate and do not use this delay.

Cleanup-candidate rules can also enable **Move Instead of Delete**. When a delete action runs for one of those candidates, Reclaimerr moves the file or folder to the configured destination and removes the source record instead of deleting the file. If multiple matched rules disagree, move wins over delete. Item-scoped movie folders move as a unit, including artwork, trailers, and all sidecars. Shared folders stay conservative: Reclaimerr moves only the selected media plus its matching sidecars (including language-tagged subtitles such as `.en.srt`) and removes the source folder only when it is empty. Existing destination folders are merged safely. When the same relative file is already present, Reclaimerr verifies matching size and SHA-256 content before discarding the redundant source copy; different files are never overwritten and leave the candidate available for review.

Move operations also honor the rule's Arr action. **Delete** removes a complete movie or series entry from Radarr/Sonarr without deleting the files that were already moved. **Unmonitor** keeps the Arr entry and refreshes its file state. Partial season and episode moves never remove the parent series entry. If Radarr or Sonarr is configured to create missing media folders, a retained unmonitored entry may recreate its empty source folder during that or a later scan; Reclaimerr does not repeatedly remove folders managed by another application.

When Leaving Soon collections are enabled, Reclaimerr first removes the affected movie or series from its managed Plex, Jellyfin, and Emby collections. This prevents media-server collections from retaining links to files that are about to disappear. If an affected collection cannot be updated, the delete or move is blocked and retried later. After the operation, Reclaimerr reconciles the collections so failed or partially completed actions remain represented.

## Deletion Modes

| Mode | Behavior |
| --- | --- |
| `delete` | Delete through Radarr, Sonarr, or the media server route used for the candidate |
| `move` | Move to the configured destination and remove the source record |
| `fallback deletion` | Use the media server when ARR cannot handle the delete path |
| `change quality profile` | Switch the Radarr or Sonarr entry onto another quality profile and keep every file |

A candidate whose rule asks for **Change Quality Profile** never enters the deletion routing below. It is handled first, resolved once the profile is applied, and recorded in reclaim history as `profile_changed`. See [Rules](rules.md#arr-action).

## Routing Order

1. Use Radarr for movie candidates when the candidate is linked to Radarr and the action can be handled there.
2. Use Sonarr for series, season, and episode candidates when the candidate is linked to Sonarr and the action can be handled there.
3. Fall back to the main media server when the ARR service cannot handle the deletion or no ARR route exists.
4. Use local deletion only when that fallback is enabled and the candidate can be removed locally.

## Movies With More Than One Version

A movie can hold several files under one Radarr entry, and a rule may match only some of them. Deleting the Radarr movie would take the rest with it, so Reclaimerr picks the narrower route instead:

- when the candidate covers **every** file in the Radarr entry, the movie is deleted through Radarr as before, which also removes the entry and can add an import list exclusion;
- when it covers **some** of them, Radarr is asked to delete just those files. The Radarr entry stays, no import exclusion is added, and the versions you kept are untouched.

Per-file deletion happens automatically and needs no setting. It also means a partial version delete no longer depends on `Allow Media Server Fallback Deletion`.

Reclaimerr only deletes a file it can identify with certainty. If a selected version cannot be matched to exactly one file in Radarr, nothing is deleted for that movie and the candidate records why.

### The Same File In Two Libraries

If one file is indexed by two libraries, your media server reports it twice, with different internal IDs and the same path. Reclaimerr keeps a record per library, so library-scoped rules keep working, but treats them as the single file they are: deleting it removes every record naming it, counts its size once, and protecting either record protects the file. A candidate whose file is protected through another library's copy is held back rather than deleted.

## What The Fallback Covers

- movie candidates that Radarr cannot remove directly
- series candidates that Sonarr cannot remove directly
- scoped deletions that need the media server to remove files or folders

## Keeping The Media Server In Step

Removing a file does not tell your media server anything. Whenever Radarr, Sonarr, or Reclaimerr itself removes media, Reclaimerr therefore reconciles the main media server afterwards:

- with `Allow Media Server Fallback Deletion` **on**, it removes the item from the media server, exactly as before;
- with it **off**, it asks the media server to re-scan the affected path instead. That is a read-only request - it never deletes anything - so the setting still means what it says.

This matters more than it sounds. Skipping the reconciliation leaves the media server serving an entry whose files are already gone. The next **Sync Media** run re-imports that entry, the next **Scan Cleanup Candidates** run flags it again as a _new_ candidate, and because the review period is measured from when a candidate was created, the countdown restarts from zero. The item then never reaches automatic deletion, however long you wait.

One Plex caveat: a path scan only drops missing items when Plex's _Empty trash automatically after every scan_ library setting is enabled, which is Plex's default. With it turned off, Plex keeps the entry until you empty the trash yourself.

## Series Held In Two Copies

A series you keep twice - an HD copy and a UHD copy, each with its own Sonarr - is stored as one set of season records, because seasons are identified by series and season number with nothing to tell the copies apart. Those season fields (size, path, resolution) describe whichever copy your main media server reported, so they cannot be used to aim a rule at one copy.

**To act on a specific copy, set the rule's Sonarr instance.** That is the only reliable way to say which one you mean.

Without it, Reclaimerr works out which Sonarr owns the files by matching paths. If the series exists in more than one Sonarr and no path matches, it refuses rather than guess, and the candidate says so - add a path mapping or set the rule's Sonarr instance. Deleting one copy also leaves the season record in place while the other copy still has files, and skips the media-server delete in favour of a path re-scan, since the stored media-server item may belong to the copy you are keeping.

## Important Settings

- `Default ARR Delete Behavior`
- `Allow Media Server Fallback Deletion`
- `Add Arr Import List Exclusions on Delete`
- `Move Destination Folders`

## Related Pages

- [How It Works](how-it-works.md)
- [Tasks](tasks.md)
- [Production](../deployment/production.md)
