# Duplicates

The **Duplicates** page has two views:

- **Media server copies**: movies and TV episodes that your main media server holds more than one file for. Pick the file to keep, and Reclaimerr removes the others.
- **Upgrade leftovers**: old downloads that Radarr replaced with an upgrade but that are still in its download folder. See [Upgrade leftovers](#upgrade-leftovers).

Media server copies come from the files your media server already reports. Episode duplicates show up after the first **Sync Media** run on this version.

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

1. If Radarr or Sonarr tracks that exact file, Reclaimerr deletes it there and rescans, so the Arr picks up the kept file. The movie or series stays in the Arr and stays monitored. Radarr and Sonarr track one file per movie or episode, so this only happens when the kept file is inside the Arr's movie or series folder. If it isn't (for example, a copy in a separate 4K library folder), the Arr would see the item as missing and download it again, so the delete is refused. Keep the Arr's file instead, or delete the other file by hand.
2. Otherwise Reclaimerr deletes it through the main media server. This needs **Allow Media Server Fallback Deletion** in General Settings.

Reclaimerr never unmonitors or removes anything in Radarr/Sonarr, and adds no import exclusion. If **Unmonitor Deleted Movies** (Radarr) or **Unmonitor Deleted Episodes** (Sonarr) is on in the Arr's Media Management settings, the Arr may unmonitor the item on its own when its file is deleted. Protected files are never deleted, and every group always keeps at least one file. Each removed file is recorded in reclaim history.

## Upgrade leftovers

When Radarr upgrades a movie, the original download can stay in the download folder. Your media server never sees it, so it is not a media server copy. Radarr's history still records where each import came from, and the **Scan Upgrade Leftovers** task uses that history to find these files.

For every movie Radarr still has, each import except the latest one has been replaced. If that older import's file is still on disk, it is listed as a leftover. The scan runs daily at 9 AM, and **Scan now** on the page runs it right away. Only Radarr is supported for now.

A file is never listed when:

- it is the same file as the movie's current library file (a hardlink of it),
- it is the latest import of any movie, on any Radarr instance,
- it sits inside a Radarr movie folder.

A leftover marked **Hardlinked, frees no space** has another hardlink somewhere else. You can still delete it, but the data stays on disk until the other link is gone too.

### Path mappings

Reclaimerr reads the download path from Radarr, so it needs to reach Radarr's download folder. If Reclaimerr runs in a different container, add a path mapping for that folder (scoped to Radarr, or global). Folders the scan couldn't reach are listed in a banner on the page.

### Manual review

- **Couldn't find the current movie file on disk to compare**: Reclaimerr can't check that the leftover isn't the current file.
- **This filesystem doesn't report hardlinks**: some network shares report no file IDs, so hardlinks can't be checked.
- **Path is a folder, not a single file**.

Use **Ignore** to hide a leftover you want to keep. It stays hidden across scans while the same file is at that path. If a different file later lands there, it shows up again.

### How leftovers are removed

Leftovers are deleted directly from disk. No Radarr or media server call is made, because neither tracks these files. Before each delete, Reclaimerr checks again that the file is the same one the scan found and that Radarr isn't using it now. After the file is deleted, its release folder is removed too, but only when the folder is named after the release and holds nothing but extras such as `.nfo` files, samples, screenshots or subtitles. Each deleted leftover is recorded in reclaim history.
