# How It Works

Reclaimerr uses the following flow:

1. Sync media and metadata from connected services.
2. Scan for reclaim candidates based on your rules.
3. Let users protect, request, or approve items.
4. Delete or move the item through the appropriate service.

See [Deletion Flow](deletion-flow.md) for routing order.
Operator reference: [Rules](rules.md).

## Candidate Scopes

Reclaimerr tracks candidates at several scopes:

- movie version
- whole movie
- whole series
- season
- episode

The delete engine uses the candidate scope to decide whether Radarr, Sonarr, or
the media server handles the action.

## Protection and Requests

- Protected media is excluded from deletion.
- Pending protection requests block automatic cleanup.
- Pending delete requests also block automatic cleanup.

## History

Successful actions are written to reclaim history with the item, timestamp, and
approval source.

