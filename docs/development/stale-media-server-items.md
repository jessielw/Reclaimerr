# Stale media server items after a delete: why we don't empty Plex trash

Decision note. Status: **not implemented, on purpose.**

## The problem

Reclaimerr removes a season's files (via Sonarr, or the folder directly), then
asks the media server to rescan the path. If the media server keeps listing the
item, the next sync re-imports it, the cleanup scan re-flags it as a **new**
candidate (review period restarts), and automatic deletion fails every run
because there is nothing left to delete.

Seen in the wild: Plex, `Lioness` S03, two Sonarr copies (HD and UHD).

- Day 0: UHD files removed, season row kept because the HD copy exists.
- 8h later: re-flagged (`Re-flagged ... 8h after Reclaimerr unmonitored`).
- Day 9: auto-delete fails with `No Sonarr episode files found for S03`.

## Why Plex still lists it

Plex does not drop an item when its file disappears. It marks it unavailable and
keeps it until the library trash is emptied. Plex has a setting, "Empty trash
automatically after every scan", on by default. Users who turned it off (often
deliberately, to survive an unmounted drive) keep the ghosts. The duplicates
feature already hit this (`_drop_missing_rows`, commit a7e8857).

## What we changed (shipped)

- Season delete: when every targeted Sonarr instance reports zero files for the
  season, none errored, and the season folder is gone from disk, the candidate is
  cleared and the path is rescanned. No more infinite failures.
- Season delete no longer falls through to a different Sonarr copy when the
  path-matched copy has nothing left (it could delete the HD copy's files).

This stops the failure loop. It does **not** stop the item being re-flagged while
Plex still lists it.

## Option A: call Plex "empty trash" after a delete - rejected

`PUT /library/sections/{id}/emptyTrash` exists and is what python-plexapi uses.

Why not:

1. **It is section-wide, not per item.** It permanently drops every unavailable
   item in the library, not only the one we just removed.
2. **Unmounted storage looks identical to deleted files.** If a NAS or mount is
   down, Plex flags large parts of the library as unavailable. Emptying trash then
   erases their metadata: watch history, ratings, collections, custom artwork.
   Reclaimerr's own rules read watch data from Plex, so this directly damages
   decisions Reclaimerr makes later. That is not recoverable.
3. **It overrides a user's deliberate choice.** Turning off automatic trash
   emptying is a common safety setting. Reclaimerr silently reversing it is not
   acceptable for a tool whose job is already destructive.
4. Jellyfin and Emby behave differently, so this would be a Plex-only special
   case to maintain.

Conclusion: the blast radius is far larger than the problem.

## Option B: skip seasons whose files are missing during sync - deferred

Filter the Plex season/episode aggregation by checking each part's file on disk,
like the duplicates fix.

Why not now:

1. Same ambiguity as above: "file missing" can mean "Reclaimerr cannot see that
   storage". The duplicates fix only drops a row when *some* sibling file exists.
   For a season whose files are **all** gone there is no sibling to compare with,
   so the check cannot tell gone from unmounted.
2. A false "gone" in sync deletes season rows, candidates and protections
   (`_sync_seasons` removes seasons missing from the incoming data), which is
   destructive and hard to undo.
3. It touches every sync for every user to fix a case that now clears itself.

Revisit if re-flagging turns out to be common. A safe version would require
Sonarr to confirm zero files **and** the disk check, the same two signals the
delete path now uses, rather than the disk alone.

## Option C: tell the user - chosen

- The existing log line already says the media server "most likely still lists
  media Reclaimerr already removed."
- Guidance for affected users: in Plex, Settings > Library, enable "Empty trash
  automatically after every scan", or empty the trash on that library manually
  once after confirming storage is mounted. The decision stays with the user.

## Related paths not yet changed

- **Episode delete** (`_delete_episode_candidates`): `No episode file in Sonarr`
  fails every run in the same situation. Same fix shape, not done.
- **Movie delete via Radarr unmonitor**: a version file already gone is reported
  as "was not found by Reclaimerr", which cannot be told apart from an unmapped
  path. Needs Radarr's `hasFile` as the authority first.
- Move/finalize season actions (monitor new seasons, list exclusion) still fail
  as before when nothing is left, since they need Sonarr to finalize monitoring.
