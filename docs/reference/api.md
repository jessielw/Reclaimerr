# API Reference

Reclaimerr exposes a FastAPI backend. The full schema is available from the
running app at:

- `GET /docs`
- `GET /redoc`
- `GET /openapi.json`

Use those generated docs for the exact request and response models.

## External API

The supported external API is versioned under `/api/v1` and uses scoped bearer
tokens. Administrators create and revoke tokens from **Settings > Integrations**.
The token secret is displayed once and cannot be recovered later.

```http
Authorization: Bearer rcl_prefix_secret
```

Available scopes are:

- `system:read` for version, capabilities, and latest successful sync times.
- `media:read` for movie and series catalog lookup.
- `candidates:read` for candidate lookup and status queries.
- `candidates:manage` for cancellation, postponement, timer resets, and
  candidate protection. This also grants `candidates:read`.
- `events:read` for the durable lifecycle event feed.
- `protections:read` for protection lookup.
- `protections:manage` for creating and removing protections. This also grants
  `protections:read`.
- `tasks:read` for schedules, next runs, current state, and run history.
- `tasks:run` for triggering enabled tasks. This also grants `tasks:read`.

`GET /api/v1` returns API discovery links and the scopes granted to the current
token. Integration clients should use these versioned endpoints instead of the
cookie-authenticated `/api/...` routes used by Reclaimerr's own UI.

### Candidate Endpoints

- `GET /api/v1/candidates`
- `GET /api/v1/candidates/{candidate_id}`
- `POST /api/v1/candidates/{candidate_id}/cancel`
- `POST /api/v1/candidates/{candidate_id}/postpone`
- `POST /api/v1/candidates/{candidate_id}/reset-timer`
- `POST /api/v1/candidates/{candidate_id}/protect`

List queries can filter by media type, TMDB ID, internal series ID, season or
episode number, and auto-delete state. Mutating requests may include an
`Idempotency-Key` header so an automation retry receives the original response
without repeating the action.

Canceling suppresses automatic deletion only while the same candidate
continuously qualifies. It does not prevent an administrator from manually
deleting it. Permanent protection uses the separate `protect` action and blocks
both automatic and manual candidate operations.

```bash
curl -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  "https://reclaimerr.example/api/v1/candidates?media_type=movie&tmdb_id=550"

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

### Events

- `GET /api/v1/events`

Events are returned oldest-first. Supply the response's `next_cursor` as the
next request's `cursor` to poll without gaps. The feed can also be filtered by
event type, candidate ID, or occurrence time. Events are persisted before
webhook delivery, so this endpoint can be used to recover activity when an
external automation was offline.

### Media

- `GET /api/v1/movies`
- `GET /api/v1/movies/{media_id}`
- `GET /api/v1/series`
- `GET /api/v1/series/{media_id}`

Media lists support title search, canonical provider IDs, status, pagination,
sorting, and optional removed-item inclusion. Responses include candidate and
active-protection IDs but intentionally omit filesystem paths and service
credentials.

### Protections

- `GET /api/v1/protections`
- `GET /api/v1/protections/{protection_id}`
- `POST /api/v1/protections`
- `DELETE /api/v1/protections/{protection_id}`

Protections can target a movie, movie version, series, season, or episode. The
root media item may be selected by its Reclaimerr ID or TMDB ID. Omitting
`expires_at` creates a permanent protection; providing a future timestamp
creates a temporary one.

```bash
curl -X POST \
  -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"media_type":"movie","tmdb_id":550,"reason":"Keep for event"}' \
  "https://reclaimerr.example/api/v1/protections"
```

### Tasks and System

- `GET /api/v1/system`
- `GET /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}`
- `GET /api/v1/tasks/{task_id}/runs`
- `POST /api/v1/tasks/{task_id}/run`

Task schedule mutation is intentionally not part of v1. External integrations
can observe schedules and trigger tasks that are already enabled, while schedule
configuration remains an administrator action in Reclaimerr.

### Lifecycle Webhooks

Lifecycle webhook endpoints are also managed from **Settings > Integrations**.
They can subscribe to candidate scheduled, canceled, postponed, timer-reset,
protected, deleted, and moved events, plus protection-created and
protection-removed events. Events and deliveries are persisted before sending,
retried with backoff after temporary network/HTTP failures, recovered after
restarts, and can be retried manually from the delivery history.

Existing post-action webhook configurations are migrated once into the new
endpoint table with only their previous deleted/moved subscriptions enabled.
The legacy editor and configuration column are then removed, leaving
**Settings > Integrations** as the single source of truth.

## Common Areas

### Status And Setup

- `GET /api/setup/status`
- `POST /api/setup`
- `GET /health`
- `GET /version`
- `GET /update-status`

### Dashboard And UI State

- `GET /api/dashboard`
- `GET /api/info/sidebar-indicators`
- `GET /api/info/ui-indicators`
- `GET /api/alerts`
- `GET /api/notices`

### Settings

- `GET /api/settings/general`
- `PUT /api/settings/general`
- `GET /api/settings/services`
- `POST /api/settings/save/service`
- `DELETE /api/settings/service/{service_config_id}`
- `POST /api/settings/notifications/test`
- `GET /api/settings/oidc`
- `GET /api/settings/integrations/api-tokens`
- `GET /api/settings/integrations/webhooks`
- `GET /api/settings/integrations/webhook-deliveries`

### Tasks

- `GET /api/tasks/tasks`
- `GET /api/tasks/tasks/{task_id}`
- `POST /api/tasks/tasks/{task_id}/run`
- `PUT /api/tasks/tasks/{task_id}/schedule`
- `GET /api/tasks/history`

### Media And Reclaim Flow

- `GET /api/media/candidates`
- `GET /api/media/candidates/presence`
- `POST /api/media/candidates/delete`
- `POST /api/media/candidates/move`
- `GET /api/media/reclaim-history`

### Requests And Protection

- `GET /api/protection-requests`
- `POST /api/protection-requests`
- `GET /api/delete-requests`
- `POST /api/delete-requests`
- `GET /api/protected`
- `POST /api/protected`
- `PUT /api/protected/{entry_id}/duration`
- `DELETE /api/protected/{entry_id}`

### Rules

- `GET /api/rules`
- `POST /api/rules`
- `POST /api/rules/{rule_id}`
- `DELETE /api/rules/{rule_id}`
- `POST /api/rules/import`
- `POST /api/rules/preview`
- `POST /api/rules/validate-regex`
- `POST /api/rules/validate-paths`
- `GET /api/rules/path-tree`
- `GET /api/rules/seerr-users`
- `GET /api/rules/movie-collections`
- `GET /api/rules/genres`
- `GET /api/rules/original-languages`
- `GET /api/rules/origin-countries`
- `GET /api/rules/media-server-collections`
- `GET /api/rules/check-synced`

The lookup endpoints are admin-only helpers used by the rule editor. Language
results use canonical ISO 639-3 codes. Country results use the codes currently
stored in local TMDB metadata. Both endpoints support media-type filtering,
search, and pagination.

Rule actions support `outcome: "candidate"` and `outcome: "protect"`. Existing
rules without an outcome remain candidate rules for compatibility. Protection
previews include items that are already protected because the preview reports
what the rule itself matches.

Protected-entry responses include `source`, `source_rule_id`, and
`source_rule_name`. Entries with `source: "rule"` are managed by cleanup scans;
duration updates and direct deletion return `409 Conflict`.

## Notes

- Most endpoints require authentication.
- Some endpoints are admin-only, especially settings and task management.
- Saving an existing service with `enabled: false` does not require the
  external service to be reachable. Enabling it still performs connection
  validation.
- Deleting the active main media server returns a conflict until another main
  server is assigned.
- Task execution and file operations are queued when appropriate; the API often
  returns a queued-job response instead of doing the work inline.
