# Support diagnostics

Administrators can share a JSON report without SQL, developer tools, changing logging, or sending a database copy.

## Create and share a report

1. For one title, open its details and choose **Debug info**. The bug button on a candidate opens its exact movie-version, season or episode scope. For app, task or database problems, open **Settings > About > Support diagnostics**.
2. Read the summary and expand **JSON preview**. For a movie or series, the scope selector lets you focus on a version, season or episode; large lists have **Previous 100** and **Next 100** controls.
3. For a mount or path-mapping problem, optionally enable **Include file paths**. This generates a new preview. Leave it off for other problems.
4. Choose **Copy debug info** or **Download JSON**, then paste or attach it to your chosen support conversation. Both export the exact displayed snapshot.

If clipboard access is blocked (including on some HTTP installations), the JSON preview opens for manual selection and the download remains available. A failed request offers **Try again**. **Refresh report** explicitly collects a new snapshot.

Generating, copying and downloading do not upload the report, create an issue, contact providers, enqueue jobs, refresh media or change settings. Reports are not saved to disk on the server. Only administrators may generate them, including through direct API requests. Downloaded files can be shared by whoever has a copy.

## What is included

Media reports contain stored IDs and titles, storage identities and path-mapping relationships, selected playback providers, native and imported evidence, bounded event samples, recorded candidate conditions, current relevant rule definitions, protection and request state. Parent decisions are included with their actual scope. A series report summarizes children rather than exporting every episode.

System reports contain app/runtime information, service aliases and provider bindings, stored refresh outcomes, library selections, relevant cleanup settings, task schedules, queued/running jobs and recent task runs. Database information includes migration revision, SQLite version, file sizes when readable, and PRAGMAs measured on a dedicated read-only diagnostics connection.

## Prepare a controlled support bundle

1. Expand **Support bundle - choose what to share** in the same dialog.
2. Choose a problem preset, then adjust sections, history window (1–30 days), and playback sample limit (10, 50 or 100). The selected media target stays explicit.
3. Optionally enable **sanitized SQLite extract** or **sanitized log summaries**. The path option above the report also applies to the bundle.
4. Choose **Prepare bundle preview**, review **Sharing inventory and limits** and **Full bundle data preview**, then **Download previewed ZIP**.

The ZIP contains `report.json`, `evidence.json`, `manifest.json`, `README.txt`, and optionally `support-extract.sqlite`. The manifest lists exact record counts, files, exclusions, collection window and limitations. The preview shows the download's SHA-256. Changing an option disables download until a new preview is prepared; downloading never silently recollects data.

The SQLite file is built fresh from the sanitized preview, using `support_*` tables with selected IDs and relationships. It contains no source database pages, WAL, sessions, credentials, triggers or arbitrary tables. **It is not a backup and cannot restore Reclaimerr.** Use the regular backup feature for recovery.

App/runtime and selected media identity are always included. Service configuration and schedules/recent task runs describe app-wide context when selected. Media timelines and decision traces are scoped to the selected item (including children for movie/series/season). System timelines cover app-wide operations. The time window limits event samples, traces, logs and timeline entries; cumulative playback totals and current candidate/configuration state can predate it. Decision conditions may contain playback evidence even with the separate playback section off. Review the actual preview, not only the selected section names.

Prepared ZIPs are kept only in server memory for up to ten minutes, bound to the creating administrator. The cache holds at most eight bundles / 16 MiB per server process and can evict earlier previews. A restart also invalidates downloads. An expired preview prompts regeneration. There is no automatic upload.

## Historical decisions and failure timelines

Open **Why this candidate?** in a media report to read saved match inputs and current protections. Choose **Load recorded scan evidence** to prepare the candidate bundle. Its readable view shows expected versus observed values, matched/unmatched/skipped conditions, captured ALL/ANY rule logic, and scan/job codes. Unknown inputs remain unknown; they are never displayed as zero.

Future cleanup scans record bounded observations of actual rule evaluations: scan/job IDs, rule revision, observed values, condition outcomes, skipped or unknown inputs, and overall results. No rule is rerun to create a bundle. Current rule definitions and captured historical definitions are separate. Protection rows describe state **after scan reconciliation**, not an invented precheck.

Older scans have no historical trace. Targets excluded by SQL filters before evaluation also have no trace. Missing traces never mean a rule failed. Retention is bounded to 1,000 evaluations / 4 MiB per scan, 5,000 stored traces, 50 scans and 14 days; pruning runs when support history is recorded. Local history may contain original rule inputs; export sanitization happens before anything is shared.

Timelines combine recorded candidate/review timestamps, correlated job events, scan results, and future move/delete operations. Batch completion/partial events do not claim that every individual item succeeded. **Per-item operation outcomes** show explicit candidate results and observed file/provider steps, including the stage and safe failure category. Earlier failures remain visible after a successful fallback. Exact failed child paths are included where the filesystem operation reports them; paths use aliases unless path sharing is enabled. A whole-directory move or a shared provider request is not presented as verified per-file success. Unrecorded final outcomes are marked unknown, including interrupted batches.

Recording is bounded to 1,000 steps and 1,000 candidate outcomes per operation, with at most 4,000 step associations plus 1,000 outcomes persisted. Limits are reported in the exported observations. Stored operation history is limited to 5,000 events and 14 days, pruned during subsequent recording. These observations are written after operational transactions; recording failure does not undo a completed operation. No operation is retried for diagnostics.

Raw errors and actor identities are excluded. Log summaries are opt-in: up to three bounded 256 KiB log tails, at most 100 summaries, with times, levels, failure categories and correlation IDs. Media reports include only correlated log entries. Original message text, tracebacks, addresses and credentials are never copied. Older uncorrelated logs may contribute nothing to a media bundle.

## Find evidence using a support code

1. Open **Settings > About > Support diagnostics**, then **Find evidence by support code**.
2. Paste an exact `job-123`, `scan-UUID`, `operation-UUID`, or bare correlation UUID from the logs or timeline. Choose **Look up code**. Recorded scan and operation views also provide lookup buttons.
3. Choose the affected target when needed, then **Prepare matching evidence**. Review the preview before downloading its ZIP.

Lookup reads stored history only. It does not rescan, retry, contact providers, or upload anything. The bundle filters historical traces, operation events, related jobs and optional logs by the exact code. Current settings, selected media and app-wide context remain separately labeled. At most 100 target choices are offered; choosing all targets still respects bundle collection limits. Logs, paths and the SQLite extract keep their existing explicit sharing controls.

No match can mean expired history or work from before recording was introduced. Unavailable history is shown separately. Matching bundles use a 30-day window; retention can leave less evidence. Detailed operation observations require the `d5f0b2c7e439` schema revision; older batch records contain no invented file steps.

## Run an explicit check

Expand **Run a diagnostic check**, choose a check, then **Run selected check**. These checks never run automatically when generating a report:

- **Stored paths and mappings:** metadata lookups of saved paths and mapped variants (at most five paths and three variants each). No crawl, file-content read, write test or repair. A successful metadata lookup does not prove write access or mount health.
- **Stored relationships:** selected media parents and configured history-source bindings. This is not a whole-database integrity scan.
- **Provider connection:** a read-only connection test against one saved service. It uses saved credentials internally and returns only a sanitized result. No arbitrary URL/path input, provider sync, library refresh or cleanup runs.

Each check phase has a five-second timeout and at most two checks can occupy worker slots. A stuck filesystem lookup retains its slot until the OS returns; timeouts report **unknown**, never success. Results have timestamps and can be downloaded separately with **Download check result**. They are not silently added to an already prepared bundle.

## Sharing and privacy

API keys, passwords, cookies, tokens, headers and complete provider responses are excluded. Server/library names and account identities use consistent aliases within a report. Different servers and accounts retain distinct identities. Free-text errors and saved display messages are omitted; structured failure flags remain. Text in rule literals is aliased while numeric/boolean evidence remains.

Reports **include media titles, canonical IDs and playback information**. They are not anonymous. Review the preview before sharing. Path opt-in may reveal folder names and media filenames; home-directory account names and UNC hostnames stay masked, and URL-like paths containing credentials or query data are omitted.

## Interpret evidence carefully

| Evidence | Meaning |
| --- | --- |
| `zero` | A stored snapshot explicitly contains zero; this is not inferred from missing history. |
| `unknown` | Available stored data cannot establish the answer. |
| `not_configured` | No selected history provider was resolved from the stored configuration. |
| `unavailable` | A section could not be collected or a provider recorded failure. The report does not perform a new provider check. |
| `stale` | Stored evidence is over 24 hours old, or its refresh time is absent. Read the timestamps; this is a diagnostic threshold. |
| `not_collected` | No stored observation is available, or the report deliberately did not perform the check. |

Legacy combined imported aggregates are not necessarily what current provider selection would use. Event samples identify their provider/server and whether they have a stored target link or only a canonical-ID association. A movie event does not prove which file version was played. Missing matches do not establish zero playback. Unmatched totals describe saved links, not a new matching pass.

Candidate conditions are recorded evidence, not a newly evaluated rule. Their creation/update timestamps are not an exact historical scan trace. Saved rule definitions are current and may have changed since a match. Reports do not claim to reconstruct missing inputs or an unrecorded enqueue exception. Future historical observations are available in the bundle's separate traces and timeline sections; this does not fill gaps in older candidate records.

Path mappings in ordinary reports describe configured relationships; file existence and mount reachability require the separate explicit check. SQLite settings describe the diagnostics connection, not every application connection. Reports cannot identify SQLite's write-lock owner and do not run checkpoints, integrity scans or write-lock probes.

## Partial reports and limits

A partial report is still useful. Expand the partial-report details to see failed sections, sample counts and omissions. Collection has a five-second budget, with up to 50 event samples, 20 recent task runs and 100 rows per other query. Child lists can be paged. Oversized saved JSON values are omitted; exports retain valid JSON within a 512 KiB limit. Two reports can generate concurrently per server process; a busy response asks you to retry. Bundles add a separate five-second history collection budget and a 1 MiB evidence cap. Timeline output is limited to 200 entries, with at most 100 traces and 100 stored operations sampled.

Observations are collected separately and carry timestamps. They are not one atomic database snapshot. A busy or missing database can leave runtime details available with database sections unavailable, **after authorization succeeds**. If the app cannot validate the administrator's session, it cannot safely issue a report; use the logs and [troubleshooting guide](../reference/troubleshooting.md).
