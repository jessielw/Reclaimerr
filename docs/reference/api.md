# External API

The supported external API is versioned under `/api/v1` and uses scoped bearer tokens. Administrators create and revoke tokens from **Settings > Integrations**. The token secret is displayed once and cannot be recovered later.

Interactive documentation containing only the supported external API is generated automatically at:

- `GET /api/v1/docs` for Swagger UI
- `GET /api/v1/redoc` for ReDoc
- `GET /api/v1/openapi.json` for the OpenAPI document

Use Swagger UI's **Authorize** button to provide a bearer token and try requests directly from the browser.

These pages load their scripts and styles from Reclaimerr itself under `/docs-assets`, not from a third-party CDN, so they also work without internet access. If your reverse proxy only forwards selected paths, include `/docs-assets` along with the docs pages.

```http
Authorization: Bearer rcl_prefix_secret
```

Available scopes are:

- `system:read` for version, capabilities, latest successful sync times, and the totals in `/api/v1/stats`.
- `media:read` for movie and series catalog lookup, including movie version paths and media server IDs.
- `candidates:read` for candidate lookup, status queries, and the unified leaving-soon feed.
- `candidates:manage` for cancellation, postponement, timer resets, and candidate protection. This also grants `candidates:read`.
- `delete_requests:read` for delete request lookup and approval options.
- `delete_requests:manage` for approving, denying, and retrying requests and changing their schedules. This also grants `delete_requests:read`.
- `scheduled_deletions:read` for standalone and request-linked schedule lookup.
- `scheduled_deletions:manage` for creating, rescheduling, and canceling standalone manual schedules. This also grants `scheduled_deletions:read`.
- `events:read` for the durable lifecycle event feed.
- `protections:read` for protection lookup.
- `protections:manage` for creating and removing protections. This also grants `protections:read`.
- `rules:read` for listing rules and evaluating match-only rules.
- `tasks:read` for schedules, next runs, current state, and run history.
- `tasks:run` for triggering enabled tasks. This also grants `tasks:read`.
- `users:read` for local account lookup, import-capable media servers, and media-user import previews.
- `users:manage` for importing non-admin accounts and explicitly linking media identities to non-admin accounts. This also grants `users:read`. Administrator accounts must be provisioned or linked through the administrator UI.

`GET /api/v1` returns API discovery links and the scopes granted to the current token. Integration clients should use these versioned endpoints instead of the cookie-authenticated `/api/...` routes used by Reclaimerr's own UI.

### User Import

- `GET /api/v1/users` lists credential-free local account summaries. Filter by `q`; paginate with `offset` (default 0) and `limit` (default 100, maximum 1,000).
- `GET /api/v1/users/import-from-media-server/providers` lists enabled media-server configurations by `service_config_id`, `service_type`, and `name` without their addresses or credentials.
- `POST /api/v1/users/import-from-media-server?preview=true` discovers current users and previews proposed changes. Preview is the default and does not create accounts or identity links.
- `POST /api/v1/users/import-from-media-server?preview=false` applies the selected batch atomically; it requires `users:manage`.

Account lookup responses include `media_identities`, an array of persisted Jellyfin, Plex, and Emby links. Each entry contains `source_service`, `source_service_config_id`, `source_service_name`, `source_user_id`, `username`, and nullable `display_name`. Use the server configuration ID together with the provider user ID to match an identity; provider user IDs alone can collide across servers. Accounts without links return `[]`. Links are returned even if a server is disabled, using the last stored names without contacting the server. No provider tokens, raw payloads, or server addresses are included.

The JSON request accepts `service_config_id`, optional `source_user_ids`, explicit `links: [{"source_user_id": "provider-id", "target_user_id": 42}]`, and access settings for **new accounts only**: `role` (API imports require `user`), `permissions` (default `["request"]`), `use_default_page_access` (default `true`), and `allowed_pages`. With default page access disabled, `allowed_pages: null` means unrestricted and `[]` means no pages. Existing accounts retain all their settings.

Omitting `source_user_ids` imports eligible new accounts and explicitly supplied links. Existing-account suggestions are skipped unless confirmed in `links`. An empty source selection changes nothing. Link entries must belong to an explicit selection when one is supplied. A media identity already linked to a different local account cannot be reassigned through import. Unsupported Plex managed profiles and disabled server users are skipped. Conflicting email/username matches require manual resolution.

Responses contain `rows` with provider user IDs, media usernames, optional email, proposed local usernames, local account summaries, `status`, `message`, and `revision`. Preview statuses are `ready_to_create`, `ready_to_link`, `needs_review`, `conflict`, or `skipped`; successful execution reports `created`, `linked`, or `skipped`. Totals include `ready`, `created`, `linked`, `skipped`, and `errors`. A new account and its identity link count once as `created`; `linked` counts links to existing accounts. Preview `skipped` includes suggestions needing confirmation; `errors` counts conflicts or errors across preview rows, including unselected users. Only selected conflicts block execution.

Copy selected rows' revision values into `expected_revisions: {"provider-id": "revision"}` on execution to reject changes since preview. The UI always does this; integrations can omit it for direct imports that still revalidate current accounts and provider data. A selected conflict, unknown/stale selection, or concurrent write returns HTTP 409 with no partial account or link changes. Provider failures return HTTP 502, not an empty successful directory. Retrying a completed import skips already linked identities. Directories are limited to 1,000 users.

```bash
# Discover configured servers.
curl -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  "https://reclaimerr.example/api/v1/users/import-from-media-server/providers"

# Preview selected users and an explicitly confirmed existing-account link.
curl -X POST -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"service_config_id":1,"source_user_ids":["new-id","existing-id"],"links":[{"source_user_id":"existing-id","target_user_id":42}]}' \
  "https://reclaimerr.example/api/v1/users/import-from-media-server?preview=true"
```

After reviewing the response, submit the same selection with `preview=false` and the selected rows' `expected_revisions`. Import does not obtain media-user tokens or add impersonation to other API endpoints. Account lookup and import responses never contain password hashes, provider credentials, or raw provider payloads.

### Candidate Endpoints

- `GET /api/v1/candidates`
- `GET /api/v1/candidates/{candidate_id}`
- `POST /api/v1/candidates/{candidate_id}/cancel`
- `POST /api/v1/candidates/{candidate_id}/postpone`
- `POST /api/v1/candidates/{candidate_id}/reset-timer`
- `POST /api/v1/candidates/{candidate_id}/protect`

List queries can filter by media type, matched cleanup rule ID (`rule_id`), TMDB ID, internal series ID, season or episode number, and auto-delete state. A rule filter matches candidates whose `matched_rule_ids` array contains that ID. Mutating requests may include an `Idempotency-Key` header so an automation retry receives the original response without repeating the action.

Candidate responses include `size_bytes`, the estimated size in bytes for the candidate's scope. A movie-version candidate reports only that version's size; movie, series, season, and episode candidates report their respective scope's size. The stored cleanup estimate takes precedence, with the scoped media size used when no estimate is available. Unknown sizes are `null`, and known zero sizes remain `0`. This field is also included in candidate action responses and newly recorded lifecycle event/webhook payloads; existing stored events are unchanged.

Canceling suppresses automatic deletion only while the same candidate continuously qualifies. It does not prevent an administrator from manually deleting it. Permanent protection uses the separate `protect` action and blocks both automatic and manual candidate operations.

```bash
curl -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  "https://reclaimerr.example/api/v1/candidates?media_type=movie&tmdb_id=550"

curl -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  "https://reclaimerr.example/api/v1/candidates?rule_id=6"

curl -X POST \
  -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: home-assistant-550-keep" \
  -d '{"reason":"Requester chose Keep"}' \
  "https://reclaimerr.example/api/v1/candidates/42/cancel"

curl -X POST \
  -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"until":"2026-08-15T12:00:00Z","reason":"Extended review period"}' \
  "https://reclaimerr.example/api/v1/candidates/42/postpone"
```

### Leaving Soon

`GET /api/v1/leaving-soon` returns cleanup candidates with automatic deletion enabled, manually scheduled deletions, and delayed approved deletion requests in one paginated list. It requires `candidates:read` (also granted by `candidates:manage`) and is linked as `leaving_soon` in API discovery.

Each item includes `source` (`candidate`, `manual`, or `delete_request`), `delete_at`, `state`, `delete_operation` (`delete` or `move`), `scope`, `size_bytes`, title, year, TMDB ID, and Reclaimerr media IDs. Movie versions include `movie_version_id`; TV scopes include series, season, and episode IDs and numbers where applicable. `candidate_id`, `scheduled_deletion_id`, and `delete_request_id` identify the originating records when present. Scheduled entries use the saved target snapshot; filesystem paths, routing settings, and account details are omitted.

`size_bytes` describes only the selected scope, using the cleanup estimate or that scope's size for candidates, and the saved estimate for schedules. Unknown sizes are `null`; known zero sizes are `0`.

Results are ordered by `delete_at` ascending, with stable source and record-ID tie breakers. Use `page` (default 1) and `per_page` (default 50, maximum 200). The response includes `items`, `total`, `page`, `per_page`, and `total_pages`. Optional filters are `media_type` (`movie` or `series`), `delete_after`, and `delete_before`. Both date bounds are inclusive ISO 8601 timestamps requiring a timezone. Filtering happens before pagination.

`delete_at` is a UTC eligibility deadline, not a guaranteed execution time: cleanup waits for the automatic deletion task, and execution still checks protection, playback, permissions, and file scope. Overdue items remain visible unless excluded by a date filter. Canceled or disabled cleanup timers, completed or canceled schedules, and quality-profile changes that keep files in place are excluded. An active manual or approved-request schedule replaces any cleanup entries it holds, using the same scope and file-overlap checks as cleanup.

Scheduled entries can have `scheduled`, `running`, `blocked`, or `failed` state; blocked and failed entries remain visible and continue holding overlapping cleanup until explicitly rescheduled or canceled. Candidate states are `scheduled`, `eligible`, or `postponed`.

```bash
curl -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  "https://reclaimerr.example/api/v1/leaving-soon?media_type=movie&delete_before=2026-12-01T00:00:00Z"
```

### Events

- `GET /api/v1/events`

Events are returned oldest-first. Supply the response's `next_cursor` as the next request's `cursor` to poll without gaps. The feed can also be filtered by event type, candidate ID, or occurrence time. Events are persisted before webhook delivery, so this endpoint can be used to recover activity when an external automation was offline.

### Media

- `GET /api/v1/movies`
- `GET /api/v1/movies/{media_id}`
- `GET /api/v1/series`
- `GET /api/v1/series/{media_id}`

Media lists support title search, canonical provider IDs, status, pagination, sorting, and optional removed-item inclusion. Responses include candidate and active-protection IDs.

Both movie endpoints include a `versions` array, ordered by Reclaimerr version ID. Each version contains `id`, `path`, `service_item_id` (the media server item ID), `service_media_id` (the media source ID), `size_bytes`, and `resolution`. Paths and resolutions are `null` when unavailable. Use `size_bytes` at the movie level for the aggregate and the version's `size_bytes` for that file.

### Delete Requests

- `GET /api/v1/delete-requests` lists requests. Filter by `status` (`pending`, `approved`, or `denied`).
- `GET /api/v1/delete-requests/{id}` shows the target, requester and linked media identities, review, and execution details.
- `GET /api/v1/delete-requests/approval-options` returns `delete_request_grace_period_days`.
- `POST /api/v1/delete-requests/{id}/approve` accepts `admin_notes` and `scheduled_at`. Send `{}` to use the configured grace period, `{"scheduled_at":null}` to queue immediately, or a future ISO 8601 timestamp with a timezone to schedule it.
- `POST /api/v1/delete-requests/{id}/deny` accepts `{}` or a body with `admin_notes`. `POST /api/v1/delete-requests/{id}/retry` retries an approved request whose immediate execution failed or was deferred. Reschedule a linked schedule to retry it.

For a token review, `reviewed_by_api_token_id` and `reviewed_by_api_token_name` identify the reviewer. Request creation and withdrawal are available through the web UI.

### Scheduled Deletions

- `GET /api/v1/scheduled-deletions` lists active standalone and request-linked schedules. Pass `active_only=false` to include completed and canceled entries.
- `GET /api/v1/scheduled-deletions/{id}` returns the saved target summary, `scheduled_at`, state, last error, and any linked `delete_request_id`.
- `POST /api/v1/scheduled-deletions` creates a standalone manual schedule for a synced target. Send `media_type`, Reclaimerr `media_id`, and a timezone-aware future `scheduled_at`. Include `movie_version_id` for one movie copy, `season_id` for a season, or both `season_id` and `episode_id` for an episode.
- `POST /api/v1/scheduled-deletions/{id}/reschedule` takes a new `scheduled_at`. `POST /api/v1/scheduled-deletions/{id}/cancel` cancels the schedule. For an approved request's schedule, both actions require `delete_requests:manage`. Standalone schedules require `scheduled_deletions:manage`.

Delete request and schedule lists use `page` (default 1) and `per_page` (default 50, maximum 200). The response includes `items`, `total`, `page`, `per_page`, and `total_pages`.

Reclaimerr checks the authorizing token when execution starts. A revoked, expired, missing, or insufficiently scoped token blocks execution. An authorized reschedule or immediate retry supplies a fresh grant. Canceling a schedule releases its hold on ordinary cleanup rules. The requested time is when the job becomes eligible to run. Playback, protections, and the saved file scope still apply.

To schedule only one copy of a movie, look up the movie, select the matching `versions[].id`, and schedule it using that value as `movie_version_id`:

```bash
curl -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  "https://reclaimerr.example/api/v1/movies?tmdb_id=550"

curl -X POST -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"media_type":"movie","media_id":42,"movie_version_id":71,"scheduled_at":"2099-06-01T16:30:00Z"}' \
  "https://reclaimerr.example/api/v1/scheduled-deletions"
```

Use the returned schedule `id` for rescheduling or cancellation.

### Protections

- `GET /api/v1/protections`
- `GET /api/v1/protections/{protection_id}`
- `POST /api/v1/protections`
- `DELETE /api/v1/protections/{protection_id}`

Protections can target a movie, movie version, series, season, or episode. Select a synced root title using exactly one of `media_id` (Reclaimerr), `tmdb_id`, `imdb_id` (for example, `tt0137523`), `tvdb_id` (series only, a numeric string), or `anilist_id`. The provider ID must already exist in the synced metadata. Ambiguous provider IDs return HTTP 409; use TMDB or Reclaimerr ID to select the intended title. Omitting `expires_at` creates a permanent protection; providing a future timestamp creates a temporary one.

`POST /api/v1/protections` reuses an active protection whose scope already covers the target rather than creating a second one, and reports `created: false` when it does. A whole-movie protection covers every version of that movie, so protecting one version of an already-protected movie returns the existing protection. An existing protection is only ever widened -- permanent wins, and an expiry only moves later.

Omit `movie_version_id`, `season_id`, and `episode_id` to protect the entire title across all libraries, including future files and episodes. Whole-title protection survives file replacement, hides fully protected duplicates by default, and blocks cleanup of associated upgrade leftovers.

A protection scoped to a movie version is removed when that file leaves the library, since it no longer protects anything. Replacing a file on disk gives it a new version ID, so re-protect the replacement if you want it kept.

```bash
curl -X POST \
  -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"media_type":"movie","tmdb_id":550,"reason":"Keep for event"}' \
  "https://reclaimerr.example/api/v1/protections"
```

### Rules

- `GET /api/v1/rules`
- `GET /api/v1/rules/{rule_id}/matches`

`/api/v1/rules` lists every rule with its `id`, `name`, `media_type`, `target_scope`, `outcome` (`candidate`, `protect`, or `match`), and `enabled`.

`/matches` evaluates one [match-only rule](../usage/rules.md#rule-outcomes) on the spot and returns every match in one response; it is not paginated. Each item carries the Reclaimerr IDs (`movie_id` and `movie_version_id`, or `series_id` with `season_number` and `episode_number` when the scope has them), `title`, `year`, `tmdb_id`, `tvdb_id`, `imdb_id`, `reason`, and an `arr` list with one `{service, service_config_id, service_name, arr_id}` entry per Radarr or Sonarr instance that has the title. Use `arr_id` directly with that instance's API. A movie with several matching versions appears once per version.

The endpoint returns HTTP 404 for an unknown rule and HTTP 409 when the rule is a cleanup or protection rule (use `/api/v1/candidates` for cleanup) or is disabled. Disabling the rule in Reclaimerr is the kill switch for any script that acts on it.

Evaluation uses the data from the latest syncs, so it can take a while on a large library. Requests are handled one at a time.

`complete` is `false` when provider data the rule depends on was missing, such as Seerr requests, playback history, or Sonarr series data. `warnings` says what was missing. **Skip the run when `complete` is false**: missing data counts as no match, so the list may be partial.

```bash
curl -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  "https://reclaimerr.example/api/v1/rules/12/matches"
```

See [Scripting Arr Changes](../usage/scripting-arr-changes.md) for complete Radarr and Sonarr scripts.

### Tasks and System

- `GET /api/v1/system`
- `GET /api/v1/stats`
- `GET /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}`
- `GET /api/v1/tasks/{task_id}/runs`
- `POST /api/v1/tasks/{task_id}/run`

`/api/v1/stats` returns flat totals for dashboard tiles. See [Dashboard Widgets](../usage/dashboard-widgets.md) for the fields and a Homepage example.

Task schedule mutation is intentionally not part of v1. External integrations can observe schedules and trigger tasks that are already enabled, while schedule configuration remains an administrator action in Reclaimerr.

### Lifecycle Webhooks

Lifecycle webhook endpoints are also managed from **Settings > Integrations**. They can subscribe to candidate scheduled, canceled, postponed, timer-reset, protected, deleted, and moved events, plus protection-created and protection-removed events. Events and deliveries are persisted before sending, retried with backoff after temporary network/HTTP failures, recovered after restarts, and can be retried manually from the delivery history.

Existing post-action webhook configurations are migrated once into the new endpoint table with only their previous deleted/moved subscriptions enabled. The legacy editor and configuration column are then removed, leaving **Settings > Integrations** as the single source of truth.

## Related Reference

Reclaimerr also has an [Internal UI API](internal-api.md) used by its own frontend. Those routes are documented for contributors and are not a supported integration contract.
