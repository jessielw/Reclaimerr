# Architecture

Reclaimerr is a FastAPI application with a small number of long-lived runtime
components:

- the API server
- the APScheduler schedule runner
- the in-process background worker
- the database and service layer

## Process Model

The app starts as one process and then initializes the main subsystems:

1. Load settings and connect to the database.
2. Bootstrap enabled media-service clients.
3. Start the scheduler.
4. Start the background worker loops.
5. Serve the API and frontend assets.

This keeps deployment constrained to one app process that manages both scheduled work and
queued background jobs.

## Request Flow

- The frontend talks to the FastAPI backend.
- Routes validate input, read or update the database, and call service helpers.
- Long-running or retryable work is placed onto the background job queue.
- The worker claims queued jobs and runs them in-process.

## Scheduler

The scheduler stores task definitions in the database and mirrors enabled tasks
into APScheduler jobs.

- Cron and interval tasks are supported.
- Manual tasks are present in the UI but are not scheduled.
- Main-server-dependent tasks stay disabled until a main media server exists.
- Task changes are persisted first, then reflected in the live scheduler.

## Background Worker

The worker polls for queued jobs and executes them one at a time per worker
loop.

- Idle polling backs off to reduce churn.
- Job claims are durable so stale jobs can be reset on startup.
- The worker handles service toggles, task runs, and file-ops jobs.

## Data Flow

Reclaimerr uses the database as the source of truth for:

- general settings
- service configuration
- task schedules
- reclaim candidates
- requests and protected media
- reclaim history and background job records

Media metadata is synced from connected services and then used to drive candidate
scanning, deletion routing, and UI indicators.

## Extension Points

- `backend/api/routes/` for HTTP endpoints
- `backend/tasks/` for domain workflows
- `backend/services/` for external integrations
- `backend/core/worker.py` for queue processing behavior
- `backend/scheduler.py` for scheduled task behavior

## Design Constraints

- Safety defaults matter more than automation.
- Candidate state must remain auditable.
- Deletion should route through the most specific service available.
- Main-server-dependent workflows must fail closed when the main server is absent.

