# API Reference

Reclaimerr exposes a FastAPI backend. The full schema is available from the
running app at:

- `GET /docs`
- `GET /redoc`
- `GET /openapi.json`

Use those generated docs for the exact request and response models.

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
- `POST /api/settings/notifications/test`
- `GET /api/settings/oidc`

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

### Rules

- `GET /api/rules`
- `POST /api/rules`
- `POST /api/rules/preview`
- `POST /api/rules/validate-regex`
- `GET /api/rules/check-synced`

## Notes

- Most endpoints require authentication.
- Some endpoints are admin-only, especially settings and task management.
- Task execution and file operations are queued when appropriate; the API often
  returns a queued-job response instead of doing the work inline.

