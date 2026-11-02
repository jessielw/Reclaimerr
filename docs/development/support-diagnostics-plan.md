# Shareable support diagnostics

Status: implemented on 2026-11-01; browser interaction QA remains pending because the development session had no browser/native-app connection. Implemented scope: media/system reports, scoped support bundles, historical decision observations, failure timelines, opt-in sanitized log summaries, and explicit checks.

## Implementation decisions

The endpoints, typed version-1 envelope, export policies, bounded collectors, shared preview/copy/download dialog, Settings entry point and scoped candidate actions are implemented. See [Support diagnostics](../usage/support-diagnostics.md) for the administrator workflow and interpretation guidance.

Synthetic example exports: [system report](examples/support-system.json) and [movie-version report](examples/support-movie.json). They are generated from an isolated fixture database, not an installation's library or credentials.

- Collection uses a dedicated SQLite URI connection with `mode=ro`, autocommit, `query_only`, a 100 ms busy timeout and a query progress deadline. It does not use the application's connection hooks, which set journal mode. Measured PRAGMAs explicitly describe **this diagnostics connection**, not the pool. Authentication still validates the administrator/session; diagnostics skip the normal session-activity write. If the database cannot authenticate the administrator, access fails rather than bypassing authorization.
- Reports expose recorded candidate conditions and current saved rule trees. They do not evaluate rules: the available preview helpers rebuild library-wide playback data. Older candidates have no historical scan trace; future scans record separate bounded observations during actual evaluations. Candidate creation/update timestamps are labelled accordingly. Free-text reasons/errors and rule names are omitted or aliased; structured numeric and boolean evidence is retained. Unknown text literals are consistently aliased.
- Native snapshots, legacy combined history aggregates, selected provider bindings and event samples are separate evidence. Combined aggregates are explicitly **not authoritative for current provider selection**. Event associations state whether they have a stored target link or only a canonical-ID match. Movie events cannot identify the exact file version played. Unmatched system counts describe persisted foreign keys, not a new reconciliation.
- Freshness uses an explicit 24-hour diagnostic threshold, with stored timestamps included; it does not claim to reproduce a task's refresh policy. File paths remain masked by default. Opt-in retains home-account/UNC-host masking and omits URL-like paths with credentials/query data. Mapped path variants are calculated with the existing pure helper; reachability is `not_collected`.
- Limits are five seconds of collection, 50 event samples, 20 recent task runs, 100 rows per other query and 512 KiB of formatted JSON. Child identities are paged in groups of 100, so administrators can reach later seasons/episodes. Saved JSON cells are capped at 64 KiB with explicit omission notices. Whole sections are omitted if needed to preserve a valid capped JSON snapshot. Two generations may run concurrently per server process; excess requests get `429` with `Retry-After`. The dialog disables regeneration controls while busy.

Focused regression coverage includes all five target scopes, same-type servers, account identity masking, nested secrets and error text, path opt-in, missing targets, busy/missing databases, deadline interruption, pagination, report size, read-only SQL, admin authorization, session revocation and admission control. Frontend compilation and backend type checks are part of validation. A manual browser pass should exercise copy denial on HTTP, download equality with the preview, keyboard focus, mobile layout and child navigation before release.

## Goal and user flow

Let an administrator share the information needed to investigate a problem without SQL, browser developer tools, environment changes, or a database copy. The experience is similar to viewing an item's underlying metadata, with a report that also explains the data Reclaimerr used to make its decisions.

1. Open **Debug info** from a media item's actions, or **Settings > About > Support diagnostics** for a system report.
2. See a short summary and a collapsible JSON preview, with **Copy debug info** and **Download JSON** buttons. Generating the report is automatic on opening.
3. Paste the report into the user's chosen support channel or attach the file.

The same preview component serves both entry points. It shows what is included, when the data was collected, and whether any sections are unavailable. Clipboard failure leaves selectable text and the download button available, including on HTTP installations where clipboard access may be restricted.

Reports stay with the user. Generating or copying one does not upload it, publish a URL, open an issue, or send a message. Logs remain a separate source for historical exceptions and execution order.

## First-release report contents

| Section | Media-item report | System report |
| --- | --- | --- |
| Context | App/build version when available, UTC capture time, schema version, report ID, selected item and scope | App/build version, runtime and deployment information that can be established reliably, UTC capture time |
| Identity and storage | Reclaimerr IDs, TMDb/TVDb/IMDb IDs, movie versions or selected season/episode, service-instance bindings, library identities, mapped-path relationships and stored availability | Enabled service types and aliases, main/linked relationships, selected history providers, library counts, path-mapping configuration summary |
| Playback evidence | Native watch values, imported event aggregates, selected provider, refresh times, field availability, unmatched/unknown reasons, bounded relevant event samples with account identities masked | Provider selection and bindings, recorded refresh outcomes, playback coverage and unmatched counts when available without a full rebuild |
| Decisions | Candidate/protection state, recorded matching reasons, relevant rule conditions and effective values, missing prerequisites and deletion/review state | Task schedules, running/queued jobs, recent completed/failed runs, queue age, relevant global configuration |
| Collection health | Freshness, unavailable sections, sampling/truncation counts and sanitized collection errors | Database migration revision, SQLite version and read-only connection PRAGMAs, database/WAL sizes when readable, limits and unavailable sections |

The target must be explicit: movie, movie version, series, season, or episode. A series report summarizes child counts; it does not export every episode's history automatically. Users can select the affected season or episode for a focused report. The candidate view should open the report at its actual scope.

Playback evidence must distinguish **zero**, **unknown**, **not configured**, **unavailable**, **stale**, and **not collected**. In particular, a native play count with no matched history is not evidence of zero plays.

Do not imply that a stored candidate is the result of a newly run rule. Label recorded reasons with their available scan time. If a rule is evaluated against current stored data, label it as a current cached-data evaluation and identify missing inputs; do not substitute it for a historical execution trace.

## Sharing defaults

Use explicit response models and an allowlist of fields. Do not serialize ORM objects, complete settings dictionaries, provider responses, exceptions, or request headers directly.

- Never export API keys, passwords/hashes, access/refresh tokens, cookies, encryption/JWT secrets, or URLs containing credentials or query tokens.
- Mask usernames, email addresses, custom server/library names, hostnames, addresses, and filesystem paths by default. Preserve equality and relationships with consistent aliases within the report, such as `server-1` and `user-2`. Alias mappings are not exported and are not stable across unrelated reports.
- Retain media titles and canonical media IDs by default because they identify the item being investigated. Explain that the report includes these and playback information; it is not anonymous merely because credentials are absent.
- Offer one optional **Include file paths** setting for path-mapping cases. Regenerate the preview when it changes. Secrets remain excluded in either mode; usernames and server credentials are never enabled by that setting.
- Sanitize nested rule literals, saved error messages, and provider text as well as field names. Prefer structured error categories over arbitrary exception strings. If a value cannot be exported safely, mark it omitted with a reason.

Default reports must retain the distinction between two servers of the same type and between two different users. Masking must not collapse identities and make a matching bug impossible to diagnose.

## Collection contract

Reports are admin-only, including direct API requests and downloads. A shared media dialog being visible to a regular user must not grant access to system configuration, other users' playback, or support exports.

The first release collects current stored state. It does not sync providers, rebuild playback aggregates, run cleanup, enqueue tasks, or change settings. Use dedicated bounded read queries; existing refresh or rule-preview entry points are not safe to invoke merely because they return useful information. Reuse pure identity/resolution and rule-evaluation code where possible.

Proposed collection limits, to validate during implementation:

- Five-second total collection budget; return useful partial sections on timeout.
- At most 50 relevant playback-event samples and 20 recent task runs.
- At most 100 child identities per report; provide totals and explicit truncation.
- A 512 KiB report cap; retain valid JSON and indicate omitted sections or samples.
- One in-flight generation per browser dialog, plus a modest server-side limit.

Close read transactions promptly. Separate observations can change during collection, so record capture start/end and per-section observation times rather than promising a globally atomic snapshot. A busy or inaccessible database must still allow an app/runtime report with database sections marked unavailable.

Database settings describe the connection measured at capture time, not every pooled connection or the state at an earlier failure. Do not claim to identify SQLite's write-lock owner. Do not run write-lock probes, checkpoints, VACUUM, integrity scans, filesystem crawls, or external health checks during collection. Path reachability is reported from stored evidence unless a separate bounded check is designed later; never label an unperformed check as successful.

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

The exact section models should be typed rather than arbitrary dictionaries. `report_id` correlates one exported report; it is not a persistent installation identifier or a hosted share link. Copy and Download use the exact displayed snapshot, not separately generated reports that can disagree.

Suggested code boundaries:

| Area | Location and approach |
| --- | --- |
| Models and routes | New `backend/models/diagnostics.py` and `backend/api/routes/diagnostics.py`; proposed admin routes `POST /api/diagnostics/system` and `POST /api/diagnostics/media` with validated target/options |
| Collection | New `backend/services/diagnostics.py`; separate system and target-scoped collectors, explicit limits, read-only state, partial results |
| Sanitization | Dedicated tested export serializer; allowlisted fields and per-report identity aliases applied before any response leaves the backend |
| Existing evidence | `playback_history.py`, `selected_playback_history.py`, media identity/selection helpers, candidate reasons, task/job models, and the pure rule evaluator; avoid refresh entry points |
| UI | Shared diagnostics dialog used by `media-detail-dialog.svelte`, scoped candidate actions, and `settings/about.svelte`; plain summary, JSON preview, copy and download |

## Delivery and acceptance

1. **Schema and sharing rules:** define typed output, aliasing, example reports, and tests with secrets placed in nested fields and error strings.
2. **System report:** implement the collector and Settings entry point. Test offline providers, a busy database, unavailable sections, and size limits.
3. **Media report:** add target-scoped identity, playback and decision evidence. Cover multiple same-type servers, missing matches, removed media, and exact movie-version/season/episode scope.
4. **Shared user flow:** add preview, copy, download and path opt-in. Verify permissions, clipboard failure, and that nontechnical users can share a report without changing logging or knowing a media/database ID.
5. **Regression and support documentation:** verify generation causes no writes, jobs or provider calls; test large libraries and concurrent work; document what each report can and cannot establish.

The implementation also includes the approved extensions below. Older missing history cannot be reconstructed, and live-provider comparisons remain outside automatic collection.

Acceptance examples from the reported incident:

- Support can see which playback provider and server binding are selected, when they last refreshed, the native versus imported values for one affected item, and why a playback field is unknown.
- A system report shows the app version, running/queued task state, recorded failures, and measured database settings without reproducing the original failure or pretending to recover an unrecorded enqueue error.
- A path report preserves source/destination mapping relationships by default; the administrator can choose to include actual paths when investigating an inaccessible mount such as `/shared`.
- Report generation remains useful if a provider is offline, and does not add a long database transaction to the problem being investigated.

## Approved support extensions (implemented)

| Capability | Implementation and boundary |
| --- | --- |
| Controlled bundle | Problem presets, explicit sections, 1–30 day window, bounded event samples and optional SQLite/logs. `POST /api/diagnostics/bundles/preview` prepares immutable bytes; `GET /api/diagnostics/bundles/{token}` serves that snapshot only to its creating admin. |
| Sanitized database | Fresh in-memory SQLite database built exclusively from typed sanitized records. No source pages/WAL, arbitrary tables, restore path or raw SQL dump. Metadata explicitly declares `restorable=false`. |
| Sharing inventory | Exact file list and logical record counts, exclusions, limits, full JSON data preview, ZIP byte size and SHA-256. In-memory cache: 10 minutes, eight entries / 16 MiB per process. |
| Recorded decisions | Migration `c4e9a1b6d328` adds scan, evaluation and operation history. The existing evaluator records actual observed conditions and short-circuit skips without a second evaluation. Rule hashes distinguish revisions; protection rows are explicitly post-reconciliation context. Limits: 1,000 evaluations / 4 MiB per scan, 5,000 traces, 50 scans, 14 days (pruned during subsequent recording). |
| Failure timeline | Selected media's candidate/review times, captured scan results, related jobs and future move/delete batch events. Correlation IDs survive isolated task execution and appear in logs. Batch outcomes never claim per-item success. |
| Opt-in log summaries | At most three 256 KiB tails / 100 structured summaries. Media exports require exact correlation tokens. Original message text and tracebacks are omitted, not regex-redacted. |
| Explicit checks | `POST /api/diagnostics/checks` accepts configured service IDs or stored media targets only. Saved path metadata, parent/history relationships and saved-provider read-only connection tests; no repair, refresh or cleanup. Two worker slots, five seconds per phase; stuck OS lookups retain their slot. Results download separately from bundles. |

Bundles have a second five-second history-query phase and a 1 MiB evidence cap. The 512 KiB report cap still applies. Current state/cumulative aggregates can predate the selected event window; selected services and task schedules/runs are app-wide context. Decision conditions can carry playback inputs even if the separate playback section is disabled. Sharing controls and documentation state these dependencies explicitly.

The schema migration was tested against an isolated temporary database, including downgrade and model/index parity. It has not been applied to the user's live data. Focused tests cover archive/preview equality, fresh SQLite integrity and counts, secret canaries, ownership/revocation/expiry, section/window controls, historical rule revisions, real short-circuit behavior, protection context and retention.

Automatic uploads, raw/full database exports, provider XML/JSON dumps, non-admin exports, replay/restore and automatic repair remain excluded.

## Support usability refinements (implemented)

1. **Readable candidate evidence:** the shared dialog renders expected/observed conditions, explicit skips and unknowns, captured ALL/ANY rule logic, scan prerequisites and post-reconciliation protections. A candidate's saved inputs remain distinct from historical evaluations. One action prepares the recorded scan evidence without reevaluating rules.
2. **Per-item operation observations:** bounded context-local recording observes existing move/delete calls and provider fallbacks. Filesystem failures retain the stage and exact child path when available; shared provider requests and whole-directory operations retain their actual scope. Explicit candidate outcomes coexist with failed intermediate steps. Missing outcomes stay unknown. Migration `d5f0b2c7e439` adds nullable typed-export detail to operation history. Persistence uses a separate transaction after operational work, stores safe failure categories rather than raw errors, and does not retry operations.
3. **Exact support-code lookup:** admin-only `POST /api/diagnostics/lookup` resolves scan, operation, job and bare correlation IDs through bounded read-only queries. The UI offers up to 100 target choices and prepares evidence filtered by the selected code. Historical filters do not remove clearly labeled current system context. Missing/unavailable/expired evidence remains explicit; logs and paths retain opt-in controls. Lookup never triggers a scan or provider request.

Regression coverage includes mixed file successes/failures, copy verification, directory child failures, provider fallback evidence, cancellation/context cleanup, unknown outcomes after interrupted work, failed history persistence, exact-code isolation, payload secrecy, path masking, authorization, and migration parity. Both migrations remain unapplied to the live database during development.
