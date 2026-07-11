# Tasks

Tasks are scheduled jobs that keep Reclaimerr running on its own.

## Common Tasks

- **Scan cleanup candidates** - evaluates media against the rule engine
- **Tag cleanup candidates** - marks candidates in the media server
- **Delete cleanup candidates** - deletes eligible candidates from rules that
  opt in to automatic deletion
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

Automatic cleanup deletion is intentionally per-rule opt-in.

To enable it:

1. Enable automatic deletion on each cleanup rule that should be allowed to
   delete candidates.
2. Enable the `Delete Cleanup Candidates` task in Tasks.
3. Review the schedule before letting it run unattended.

New installations default to a daily 2 AM delete task, with the task disabled.
Upgrades keep the schedule that is already configured.

Candidates must come from at least one auto-delete-enabled rule and finish
their review period. The default review periods are 14 days for movies and 7
days for TV. An auto-delete-enabled rule can override the delay; when several
auto-delete-enabled rules match, the longest applicable delay wins. A delay of
`0` makes the candidate immediately eligible. The exact eligibility time is
calculated from when the candidate was first created, so changing the current
delay recalculates existing candidates without resetting their clock. The task
deletes an eligible candidate on its next run.

Reclaimerr skips anything with:

- active protection
- pending protection requests
- pending delete requests

The task summary reports opted-in candidates still in their review period as
`waiting` and protected, non-opted, or otherwise blocked candidates as
`skipped`.

Manual delete and move actions bypass the automatic review period. If a cleanup
scan removes a candidate because it no longer matches, a later match creates a
new candidate with a new review-period clock.

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
