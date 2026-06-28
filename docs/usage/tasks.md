# Tasks

Tasks are scheduled jobs that keep Reclaimerr running on its own.

## Common Tasks

- **Scan cleanup candidates** - evaluates media against the rule engine
- **Tag cleanup candidates** - marks candidates in the media server
- **Delete cleanup candidates** - deletes eligible candidates when you opt in
- **Sync media** - refreshes connected services and libraries
- **Refresh IMDb Ratings** - refreshes the IMDb dataset cache
- **Refresh AniList Ratings** - refreshes AniBridge mappings and AniList metadata
- **Refresh MDBList Ratings** - refreshes Rotten Tomatoes, Metacritic, Trakt,
  and Letterboxd values supplied by MDBList
- **Refresh OMDb Ratings** - refreshes Tomatometer and Metacritic fallback values
  without replacing values available from MDBList

The Tasks page groups these four jobs under **External Ratings**. MDBList and
OMDb have independent schedules and refresh state; their default schedules are
6 AM and 7 AM respectively.

## Automatic Cleanup Deletion

Automatic cleanup deletion is intentionally opt-in.

To enable it:

1. Turn on the global opt-in in General Settings.
2. Enable the `Delete Cleanup Candidates` task in Tasks.
3. Review the schedule before letting it run unattended.

Reclaimerr skips anything with:

- active protection
- pending protection requests
- pending delete requests

The deletion flow is:

1. Radarr for movie candidates when available.
2. Sonarr for series, season, and episode candidates when available.
3. The main media server when the ARR route cannot handle the deletion.
4. Local deletion when fallback deletion is enabled.

See [Deletion Flow](deletion-flow.md) for the full routing order and delete
modes.

## Manual Vs Scheduled Runs

- Manual runs are available for most tasks, but they still respect task-level
  guards.
- Tasks that require a main media server stay disabled until one is configured.
