# External API

The supported external API is versioned under `/api/v1` and uses scoped bearer
tokens. Administrators create and revoke tokens from **Settings > Integrations**.
The token secret is displayed once and cannot be recovered later.

Interactive documentation containing only the supported external API is
generated automatically at:

- `GET /api/v1/docs` for Swagger UI
- `GET /api/v1/redoc` for ReDoc
- `GET /api/v1/openapi.json` for the OpenAPI document

Use Swagger UI's **Authorize** button to provide a bearer token and try requests
directly from the browser.

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

## Related Reference

Reclaimerr also has an [Internal UI API](internal-api.md) used by its own
frontend. Those routes are documented for contributors and are not a supported
integration contract.
