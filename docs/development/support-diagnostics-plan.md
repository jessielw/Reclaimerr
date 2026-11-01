# Shareable support diagnostics

Status: planned; implementation has not started. First-release scope agreed on
2026-11-01: media-item diagnostics plus a system report in Settings.

## Goal and user flow

Let an administrator share the information needed to investigate a problem
without SQL, browser developer tools, environment changes, or a database copy.
The experience is similar to viewing an item's underlying metadata, with a
report that also explains the data Reclaimerr used to make its decisions.

1. Open **Debug info** from a media item's actions, or **Settings > About >
   Support diagnostics** for a system report.
2. See a short summary and a collapsible JSON preview, with **Copy debug info**
   and **Download JSON** buttons. Generating the report is automatic on opening.
3. Paste the report into the user's chosen support channel or attach the file.

The same preview component serves both entry points. It shows what is included,
when the data was collected, and whether any sections are unavailable. Clipboard
failure leaves selectable text and the download button available, including on
HTTP installations where clipboard access may be restricted.

Reports stay with the user. Generating or copying one does not upload it,
publish a URL, open an issue, or send a message. Logs remain a separate source
for historical exceptions and execution order.

## First-release report contents

| Section | Media-item report | System report |
| --- | --- | --- |
| Context | App/build version when available, UTC capture time, schema version, report ID, selected item and scope | App/build version, runtime and deployment information that can be established reliably, UTC capture time |
| Identity and storage | Reclaimerr IDs, TMDb/TVDb/IMDb IDs, movie versions or selected season/episode, service-instance bindings, library identities, mapped-path relationships and stored availability | Enabled service types and aliases, main/linked relationships, selected history providers, library counts, path-mapping configuration summary |
| Playback evidence | Native watch values, imported event aggregates, selected provider, refresh times, field availability, unmatched/unknown reasons, bounded relevant event samples with account identities masked | Provider selection and bindings, recorded refresh outcomes, playback coverage and unmatched counts when available without a full rebuild |
| Decisions | Candidate/protection state, recorded matching reasons, relevant rule conditions and effective values, missing prerequisites and deletion/review state | Task schedules, running/queued jobs, recent completed/failed runs, queue age, relevant global configuration |
| Collection health | Freshness, unavailable sections, sampling/truncation counts and sanitized collection errors | Database migration revision, SQLite version and read-only connection PRAGMAs, database/WAL sizes when readable, limits and unavailable sections |

The target must be explicit: movie, movie version, series, season, or episode.
A series report summarizes child counts; it does not export every episode's
history automatically. Users can select the affected season or episode for a
focused report. The candidate view should open the report at its actual scope.

Playback evidence must distinguish **zero**, **unknown**, **not configured**,
**unavailable**, **stale**, and **not collected**. In particular, a native play
count with no matched history is not evidence of zero plays.

Do not imply that a stored candidate is the result of a newly run rule. Label
recorded reasons with their available scan time. If a rule is evaluated against
current stored data, label it as a current cached-data evaluation and identify
missing inputs; do not substitute it for a historical execution trace.

## Sharing defaults

Use explicit response models and an allowlist of fields. Do not serialize ORM
objects, complete settings dictionaries, provider responses, exceptions, or
request headers directly.

- Never export API keys, passwords/hashes, access/refresh tokens, cookies,
  encryption/JWT secrets, or URLs containing credentials or query tokens.
- Mask usernames, email addresses, custom server/library names, hostnames,
  addresses, and filesystem paths by default. Preserve equality and relationships
  with consistent aliases within the report, such as `server-1` and `user-2`.
  Alias mappings are not exported and are not stable across unrelated reports.
- Retain media titles and canonical media IDs by default because they identify
  the item being investigated. Explain that the report includes these and
  playback information; it is not anonymous merely because credentials are absent.
- Offer one optional **Include file paths** setting for path-mapping cases.
  Regenerate the preview when it changes. Secrets remain excluded in either mode;
  usernames and server credentials are never enabled by that setting.
- Sanitize nested rule literals, saved error messages, and provider text as well
  as field names. Prefer structured error categories over arbitrary exception
  strings. If a value cannot be exported safely, mark it omitted with a reason.

Default reports must retain the distinction between two servers of the same
type and between two different users. Masking must not collapse identities and
make a matching bug impossible to diagnose.

## Collection contract

Reports are admin-only, including direct API requests and downloads. A shared
media dialog being visible to a regular user must not grant access to system
configuration, other users' playback, or support exports.

The first release collects current stored state. It does not sync providers,
rebuild playback aggregates, run cleanup, enqueue tasks, or change settings.
Use dedicated bounded read queries; existing refresh or rule-preview entry
points are not safe to invoke merely because they return useful information.
Reuse pure identity/resolution and rule-evaluation code where possible.

Proposed collection limits, to validate during implementation:

- Five-second total collection budget; return useful partial sections on timeout.
- At most 50 relevant playback-event samples and 20 recent task runs.
- At most 100 child identities per report; provide totals and explicit truncation.
- A 512 KiB report cap; retain valid JSON and indicate omitted sections or samples.
- One in-flight generation per browser dialog, plus a modest server-side limit.

Close read transactions promptly. Separate observations can change during
collection, so record capture start/end and per-section observation times rather
than promising a globally atomic snapshot. A busy or inaccessible database must
still allow an app/runtime report with database sections marked unavailable.

Database settings describe the connection measured at capture time, not every
pooled connection or the state at an earlier failure. Do not claim to identify
SQLite's write-lock owner. Do not run write-lock probes, checkpoints, VACUUM,
integrity scans, filesystem crawls, or external health checks during collection.
Path reachability is reported from stored evidence unless a separate bounded
check is designed later; never label an unperformed check as successful.

## Proposed implementation

Use a versioned JSON envelope shared by both report types:

```json
{
  "schema_version": 1,
  "report_kind": "media",
  "report_id": "generated-per-report",
  "generated_at": "2026-11-01T21:00:00Z",
  "app": { "version": "example" },
  "target": { "scope": "movie_version", "id": 123 },
  "summary": [],
  "sections": {},
  "collection": { "partial": false, "omitted": [], "truncated": [] },
  "sharing": { "file_paths_included": false, "masked_categories": [] }
}
```

The exact section models should be typed rather than arbitrary dictionaries.
`report_id` correlates one exported report; it is not a persistent installation
identifier or a hosted share link. Copy and Download use the exact displayed
snapshot, not separately generated reports that can disagree.

Suggested code boundaries:

| Area | Location and approach |
| --- | --- |
| Models and routes | New `backend/models/diagnostics.py` and `backend/api/routes/diagnostics.py`; proposed admin routes `POST /api/diagnostics/system` and `POST /api/diagnostics/media` with validated target/options |
| Collection | New `backend/services/diagnostics.py`; separate system and target-scoped collectors, explicit limits, read-only state, partial results |
| Sanitization | Dedicated tested export serializer; allowlisted fields and per-report identity aliases applied before any response leaves the backend |
| Existing evidence | `playback_history.py`, `selected_playback_history.py`, media identity/selection helpers, candidate reasons, task/job models, and the pure rule evaluator; avoid refresh entry points |
| UI | Shared diagnostics dialog used by `media-detail-dialog.svelte`, scoped candidate actions, and `settings/about.svelte`; plain summary, JSON preview, copy and download |

## Delivery and acceptance

1. **Schema and sharing rules:** define typed output, aliasing, example reports,
   and tests with secrets placed in nested fields and error strings.
2. **System report:** implement the collector and Settings entry point. Test
   offline providers, a busy database, unavailable sections, and size limits.
3. **Media report:** add target-scoped identity, playback and decision evidence.
   Cover multiple same-type servers, missing matches, removed media, and exact
   movie-version/season/episode scope.
4. **Shared user flow:** add preview, copy, download and path opt-in. Verify
   permissions, clipboard failure, and that nontechnical users can share a report
   without changing logging or knowing a media/database ID.
5. **Regression and support documentation:** verify generation causes no writes,
   jobs or provider calls; test large libraries and concurrent work; document
   what each report can and cannot establish.

Initial estimate: 3-5 development days for these two report types, including
focused tests and UI validation. Broader live-provider comparisons or complete
historical rule traces would need separate scope.

Acceptance examples from the reported incident:

- Support can see which playback provider and server binding are selected,
  when they last refreshed, the native versus imported values for one affected
  item, and why a playback field is unknown.
- A system report shows the app version, running/queued task state, recorded
  failures, and measured database settings without reproducing the original
  failure or pretending to recover an unrecorded enqueue error.
- A path report preserves source/destination mapping relationships by default;
  the administrator can choose to include actual paths when investigating
  an inaccessible mount such as `/shared`.
- Report generation remains useful if a provider is offline, and does not add
  a long database transaction to the problem being investigated.

Automatic uploads, full database exports, raw provider XML/JSON dumps, non-admin
exports, and full-installation support bundles are deferred beyond this release.
