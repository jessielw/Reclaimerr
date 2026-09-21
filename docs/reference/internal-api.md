# Internal UI API

!!! warning "Not a supported integration API"

    These cookie-authenticated routes are implementation details used by the
    Reclaimerr frontend. They may change without API-version or compatibility
    guarantees. External integrations should use the supported
    [External API](api.md) under `/api/v1`.

The running application exposes its complete FastAPI schema at:

- `GET /docs`
- `GET /redoc`
- `GET /openapi.json`

These generated documents include internal UI routes as well as the mounted external API. Contributors can use them for the exact internal request and response models.

## Status And Setup

- `GET /api/setup/status`
- `POST /api/setup`
- `GET /health`
- `GET /version`
- `GET /update-status`

## Dashboard And UI State

- `GET /api/dashboard`
- `GET /api/calendar`
- `GET /api/storage`
- `GET /api/info/sidebar-indicators`
- `GET /api/info/ui-indicators`
- `GET /api/alerts`
- `GET /api/notices`

## Settings

- `GET /api/settings/general`
- `PUT /api/settings/general`
- `GET /api/settings/services`
- `POST /api/settings/save/service`
- `DELETE /api/settings/service/{service_config_id}`
- `POST /api/settings/notifications/test`
- `GET /api/settings/notifications/email-status`
- `GET /api/settings/smtp`
- `PUT /api/settings/smtp`
- `POST /api/settings/smtp/test`
- `GET /api/settings/smtp/coverage`
- `POST /api/settings/smtp/enable-all`
- `GET /api/settings/oidc`
- `GET /api/settings/integrations/api-tokens`
- `GET /api/settings/integrations/webhooks`
- `GET /api/settings/integrations/webhook-deliveries`
- `GET /api/settings/logs`
- `GET /api/settings/logs/files`
- `GET /api/settings/logs/download`

## Tasks

- `GET /api/tasks/tasks`
- `GET /api/tasks/tasks/{task_id}`
- `POST /api/tasks/tasks/{task_id}/run`
- `PUT /api/tasks/tasks/{task_id}/schedule`
- `GET /api/tasks/history`

## Media And Reclaim Flow

- `GET /api/media/candidates`
- `GET /api/media/candidates/rules`
- `GET /api/media/candidates/presence`
- `POST /api/media/candidates/delete`
- `POST /api/media/candidates/move`
- `GET /api/media/reclaim-history`

The candidate list accepts `rule_id` to match exact membership in a candidate's `matched_rule_ids` array. The candidate rule-options endpoint returns enabled and disabled cleanup-candidate rules available to users with Candidates-page access; protection-only rules are omitted.

## Requests And Protection

- `GET /api/protection-requests`
- `POST /api/protection-requests`
- `GET /api/delete-requests`
- `POST /api/delete-requests`
- `GET /api/protected`
- `POST /api/protected`
- `PUT /api/protected/{entry_id}/duration`
- `DELETE /api/protected/{entry_id}`

## Rules

- `GET /api/rules`
- `POST /api/rules`
- `POST /api/rules/{rule_id}`
- `DELETE /api/rules/{rule_id}`
- `POST /api/rules/import`
- `POST /api/rules/preview`
- `POST /api/rules/validate-regex`
- `POST /api/rules/validate-paths`
- `GET /api/rules/path-tree`
- `GET /api/rules/quality-profiles`
- `GET /api/rules/seerr-users`
- `GET /api/rules/playback-users`
- `GET /api/rules/requester-watch-explain`
- `GET /api/rules/movie-collections`
- `GET /api/rules/genres`
- `GET /api/rules/original-languages`
- `GET /api/rules/origin-countries`
- `GET /api/rules/media-server-collections`
- `GET /api/rules/check-synced`

`GET /api/rules/requester-watch-explain` reports how `Seerr requester has watched` resolves for one item: the requesters, the identities tried for each of them, the completed watches found, the episodes required, and any media server that could not report completion. It takes `media_type` and `tmdb_id`, plus `target_scope`, `season_number`, and `episode_number` for TV targets. Its verdict is produced by the same code a cleanup scan uses.

The lookup endpoints are admin-only helpers used by the rule editor. Language results use canonical ISO 639-3 codes. Country results use the codes currently stored in local TMDB metadata. Both endpoints support media-type filtering, search, and pagination.

`GET /api/calendar` groups scheduled candidate actions by day. It takes `start` and `end` (inclusive), plus `tz_offset_minutes` so days are bucketed in the viewer's own zone rather than UTC, and `per_day_limit` to cap the items returned per day; `item_count` and `total_bytes` always describe the whole day. Only candidates whose deadline is scheduled, eligible, or postponed appear, and deadlines are resolved with the same code the candidates list and the auto-delete task use. The window is capped at 366 days.

`GET /api/storage` reports disk capacity from the configured Radarr and Sonarr instances, library size, reclaimable space, and reclaimed totals by outcome. A volume reported by several instances is merged on path and total size and counted once. A mount that reports no total sets `capacity_incomplete`. An instance that cannot be reached is listed in `errors` instead of failing the request.

The log endpoints are admin-only. `GET /api/settings/logs` returns the most recent entries oldest-first, with `level` filtering to that level and above and `search` matching anywhere in an entry, both applied across the whole window that is read rather than only the returned lines. A traceback stays attached to the entry that raised it. `scan_truncated` reports that the window did not reach the start of the file. `file` accepts only the names `GET /api/settings/logs/files` returns.

`GET /api/rules/quality-profiles` lists the profiles a rule can switch media onto, for one `service` (`radarr` or `sonarr`) and optionally one `service_config_id`. Profile IDs are per instance, so each profile carries the instance it belongs to, and an unreachable instance is reported in `errors` rather than raised.

Rule actions support `outcome: "candidate"` and `outcome: "protect"`. Existing rules without an outcome remain candidate rules for compatibility. Protection previews include items that are already protected because the preview reports what the rule itself matches.

A rule's `arr_action` is one of `delete`, `unmonitor`, `unmonitor_only`, or `change_quality_profile`. The last one also requires `quality_profile_id` and accepts `trigger_search`, and is refused on season and episode scopes because a Sonarr quality profile covers the whole series. It clears `media_server_action` and `move_instead_of_delete`, since nothing is removed or relocated.

Protected-entry responses include `source`, `source_rule_id`, and `source_rule_name`. Entries with `source: "rule"` are managed by cleanup scans; duration updates and direct deletion return `409 Conflict`. Movie entries scoped to a single file also carry `version_file_name`, `version_resolution`, `version_size`, `version_video_codec`, `version_hdr`, and `version_dolby_vision`, so the page can name the file a protection covers.

`GET /api/protected` lists only protections that are still in force. An expired entry protects nothing -- every enforcement query already ignores it -- so it is omitted from both the page and its total.

Protection scopes overlap rather than match exactly. A whole-movie protection already covers every version of that movie, so `POST /api/protected` and `POST /api/protection-requests` refuse a per-version protection on top of one, the same way a series-wide protection has always refused a season-scoped one. Approving a protection request reuses an overlapping protection and widens it if the approval grants longer, instead of creating a second entry.

## Internal Behavior Notes

- Most routes require the frontend's session-cookie authentication.
- Some routes are admin-only, especially settings and task management.
- Saving an existing service with `enabled: false` does not require the external service to be reachable. Enabling it still performs connection validation.
- Deleting the active main media server returns a conflict until another main server is assigned.
- Task execution and file operations are queued when appropriate, so internal routes often return a queued-job response instead of completing work inline.
