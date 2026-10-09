# Dashboard Widgets

`GET /api/v1/stats` returns flat totals that self-hosted dashboards such as [Homepage](https://gethomepage.dev) can show as a tile. The reclaim numbers are computed the same way as Reclaimerr's own Dashboard page.

## Create a Token

1. Open **Settings > Integrations** and create an API token with only the `system:read` scope.
2. Copy the token. It is shown once.

`system:read` exposes totals only, never titles or file paths.

## Response

```json
{
  "candidates_total": 42,
  "candidates_movies": 30,
  "candidates_series": 12,
  "reclaimable_bytes": 1932735283200,
  "reclaimed_bytes_total": 5497558138880,
  "reclaimed_items_total": 311,
  "pending_protection_requests": 2,
  "pending_delete_requests": 0,
  "next_auto_delete_at": "2026-10-12T02:00:00Z",
  "auto_delete_task_enabled": true
}
```

| Field | Meaning |
| --- | --- |
| `candidates_total`, `candidates_movies`, `candidates_series` | Flagged titles. A movie with two flagged versions, or a series with three flagged seasons, counts once. |
| `reclaimable_bytes` | Space the current candidates would free. |
| `reclaimed_bytes_total`, `reclaimed_items_total` | Everything recorded in History. |
| `pending_protection_requests`, `pending_delete_requests` | Requests waiting for an admin. |
| `next_auto_delete_at` | The earliest automatic deletion or move, or `null` when nothing is scheduled. It can be in the past when an item is already eligible and waiting for the next **Delete cleanup candidates** run. |
| `auto_delete_task_enabled` | Whether the **Delete cleanup candidates** task is enabled. When it is `false`, nothing is deleted automatically, whatever `next_auto_delete_at` says. |

## Homepage Example

Add this to Homepage's `services.yaml`, replacing the URL and token:

```yaml
- Media:
    - Reclaimerr:
        href: https://reclaimerr.example
        description: Media cleanup
        widget:
          type: customapi
          url: https://reclaimerr.example/api/v1/stats
          refreshInterval: 300000 # 5 minutes
          headers:
            Authorization: Bearer rcl_prefix_secret
          mappings:
            - field: candidates_total
              label: Candidates
              format: number
            - field: reclaimable_bytes
              label: Reclaimable
              format: bytes
            - field: reclaimed_bytes_total
              label: Reclaimed
              format: bytes
            - field: next_auto_delete_at
              label: Next delete
              format: relativeDate
```

Homepage shows up to four fields per tile. Swap any of them for another field from the table above.

If the tile shows an error, open `https://reclaimerr.example/api/v1/stats` with the same header from the Homepage host, for example with `curl -H "Authorization: Bearer ..."`. A `401` means the token is missing, wrong, revoked, or expired. A `403` means it lacks `system:read`.

## Related Pages

- [External API](../reference/api.md)
