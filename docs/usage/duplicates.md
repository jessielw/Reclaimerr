# Duplicates

The **Duplicates** page lists movies and TV episodes that your main media server holds more than one file for. Pick the file to keep, and Reclaimerr removes the others.

Duplicates come from the files your media server already reports. Reclaimerr does not scan your disks. Episode duplicates show up after the first **Sync Media** run on this version.

## What counts as a duplicate

- A movie or episode with two or more **different files**. The same file indexed in two libraries is one file, not a duplicate.
- **Copies in different libraries**, such as a separate 4K library next to an HD library, are usually intentional. They are hidden until you turn on **Include copies in different libraries**.

## Picking the keeper

Each group starts with a suggested keeper, which is the best file according to the **Keeper preference** (admins only, on the page header). The rules are checked in order, and the first one that tells two files apart decides. If every rule ties, the newest file wins.

| Rule                       | Prefers                               |
| -------------------------- | ------------------------------------- |
| Higher resolution          | 2160p over 1080p over 720p            |
| Dolby Vision               | Dolby Vision over no Dolby Vision     |
| HDR                        | HDR (including Dolby Vision) over SDR |
| Newer video codec          | AV1 over HEVC over H.264              |
| More audio channels        | 7.1 over 5.1 over stereo              |
| Higher video bitrate       | The higher bitrate                    |
| Larger file / Smaller file | Off by default. Only one can be on    |

You can always pick a different keeper by hand before deleting.

## Manual review

Some groups cannot be cleaned up from Reclaimerr safely. They show a **Manual review** reason and their delete button is disabled:

- **File contains multiple episodes**: one of the files covers more than one episode (for example `S01E01-E02.mkv`), so deleting it would remove another episode too.
- **Server can't delete a single version of this item**: Jellyfin and Emby delete whole items, and here several files share one item.
- **File info incomplete**: the server reported no path or no size for a file.

Use **Not a duplicate** to hide a group you want to keep as it is. It comes back on its own if its files change.

## How files are removed

For each file you remove:

1. If Radarr or Sonarr tracks that exact file, Reclaimerr deletes it there. The movie or series stays in the Arr and stays monitored, because it still has the kept file.
2. Otherwise Reclaimerr deletes it through the main media server. This needs **Allow Media Server Fallback Deletion** in General Settings.

Nothing is unmonitored or removed from Radarr/Sonarr, and no import exclusion is added. Protected files are never deleted, and every group always keeps at least one file. Each removed file is recorded in reclaim history.
