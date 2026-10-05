# How It Works

Reclaimerr uses the following flow:

1. Sync media and metadata from connected services.
2. Scan for reclaim candidates based on your rules.
3. Let users protect, request, or approve items.
4. Delete or move the item through the appropriate service.

See [Deletion Flow](deletion-flow.md) for routing order. Operator reference: [Rules](rules.md).

## Candidate Scopes

Reclaimerr tracks candidates at several scopes:

- movie version
- whole movie
- whole series
- season
- episode

The delete engine uses the candidate scope to decide whether Radarr, Sonarr, or the media server handles the action.

## Protection and Requests

- Protected media is excluded from deletion.
- Pending protection requests block automatic cleanup.
- Pending delete requests also block automatic cleanup.

### Protect an entire title

Choose **Entire title - all libraries and versions** in a movie's Protect dialog, or protect the whole series. This covers current and future copies in every library, regardless of file fingerprints, quality, or replacement files. Whole-series protection also covers future seasons and episodes.

Managers can use **Protected → Protect title by ID** with a TMDB, IMDb, AniList, or series TVDB ID already present in synced metadata. This creates permanent protection; its duration can be changed on the Protected page. IDs select the catalog title, so different providers lead to the same protection. Titles that have not been synced cannot be added this way.

On **Duplicates**, **Protect entire title** protects the movie or the episode's entire series. Fully protected groups and associated movie upgrade leftovers are hidden by default; enable **Show ignored and protected** to see them. Protection is enforced again before deletion. **Not a duplicate** remains a separate, temporary dismissal that resets when the files change.

## History

Successful actions are written to reclaim history with the item, timestamp, and approval source.
