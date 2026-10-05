# Feature handoff: playback protection, rule presets, and rule exclusions

Playback protection has been implemented in the current working tree. Curated rule presets are next; discuss exclusions for individual rules before implementing them.

Written on 2026-10-05 after comparing this repository with Maintainerr's current documentation. The original proposals below are retained as design context. The implementation update records the decisions accepted in the continuation.

## Implementation update: 2026-10-05

Active-playback protection now uses native Plex, Jellyfin, and Emby sessions across candidate deletion/moves, duplicate cleanup, and upgrade leftovers. The global setting defaults on for new and existing installations. Playing and paused sessions defer affected files; unavailable playback data defers associated operations. Standalone unmonitor and quality-profile actions remain allowed.

Deferrals preserve candidate deadlines and approvals and have separate API/job counters and UI feedback. Approved requests retain their candidates across rule scans and provide a manager-only retry endpoint. Scheduled candidates retry on the next cleanup run; manual actions and approved requests require an explicit retry. Snapshots expire after 30 seconds; session reads have a 10-second total timeout per server. Later deferrals report any already-completed steps.

The implementation includes an Alembic migration, native adapter/identity/workflow tests, settings UI, candidate/history feedback, and request retry. The full regression run passed 1,357 tests and 81 subtests. Subsequent refinements passed a 176-test focused suite, followed by 90 passing tests after the final cross-server path and cleanup changes. Backend type checking and linting, frontend checking/build, and whitespace checks passed. Browser controls were unavailable in this session, so visual verification remains outstanding.

The implementation cannot eliminate playback starting after a check. Cross-server physical identity still depends on native identities, supplemental associations, and configured path mappings. Presets and rule exclusions have not been implemented. Collections per rule and poster overlays remain out of scope.

## User intent and boundaries

1. **Active-playback protection:** develop the idea of preventing cleanup from interrupting someone watching media.
2. **Rule presets:** develop an easier starting point for creating useful rules, inspired by Maintainerr's community rules.
3. **Exclusions for individual rules:** explain the feature and its tradeoffs. The user is undecided about it.
4. **Out of scope:** collections per rule and countdown overlays on posters. The user explicitly wants these left out for now; do not introduce them as dependencies.
5. **Current authorization:** the continuation authorized implementation of playback protection. Presets and exclusions remain proposals requiring their own decisions.

The preceding work added a Sonarr season action that unmonitors and removes files, then enables series and new-season monitoring only when the latest known regular season was removed. Successful moves also qualify. It includes history feedback and tests. Preserve that work; do not implement it again.

Reclaimerr already has rule preview, import/export, automated protection rules, manual protections, postponements, a calendar, custom collection artwork, and quality-profile changes. These should be reused where relevant.

## 1. Protect media during active playback

### Problem and expected experience

A deletion deadline can arrive while someone is watching the affected media. Recent watch history and live playback are different signals: an imported history snapshot cannot reliably tell us what is playing right now.

Maintainerr defers actively streamed media to a later collection-handler run, based on a snapshot taken once per run. This is a useful precedent, with a documented race if playback starts after that snapshot. [Maintainerr collection handling](https://docs.maintainerr.info/collections/)

Reclaimerr has playback history integrations, but the inspected service clients and cleanup paths do not expose a comparable live-session guard. Confirm this against the current checkout before adding one.

Proposed user experience: a candidate says **“Deferred: currently playing”** and stays queued. Once playback ends, it becomes actionable on a later run without starting its entire review period again.

### Recommended first implementation

1. Fetch live playback from Plex, Jellyfin, and Emby through their native service clients. Introduce a shared result that distinguishes a successful empty session list from unavailable or invalid session data. Verify the providers' current API contracts before implementing adapters.
2. Match sessions to the physical files an operation would remove. Use server/configuration identity plus media IDs and mapped paths; avoid title matching. Whole-series and season actions must account for playing descendant episodes. A file containing multiple episodes must be protected as one physical file.
3. Enforce the guard in the common candidate workflow for scheduled deletion, manual deletion, and moves, before unmonitoring, removing collection membership, or changing files. Keep non-removal quality-profile actions separate. Audit API, request-approval, and background-job entry points for bypasses.
4. Defer affected items with an understandable reason and count; continue unrelated work. Do not record a deletion, remove the candidate, or reset its review timer. For a server whose live state cannot be read, the recommended behavior is to defer operations that could touch that server's files.
5. Batch session reads by server and refresh during long jobs. A proposed maximum snapshot age is 30 seconds before a destructive batch. This reduces requests without treating a snapshot from the start of a long job as current; it cannot eliminate every race with playback starting afterward.

Treat paused sessions as active initially: someone can resume them. Avoid depending on Tautulli or Tracearr merely to make this guard available.

### Decisions to settle before coding

| Decision | Recommended starting point |
| --- | --- |
| Default setting | Enabled, with a clear global setting; discuss rollout for existing installs. |
| Manual actions | Respect the guard. No override in the first version. |
| Multiple media servers | Check every configured server known to expose the affected physical files, including supplemental servers. An unrelated server outage should not stop unrelated cleanup. |
| Stale or paused sessions | Keep them protected while the provider reports them; expose the reason rather than guessing a playback timeout. |
| Duplicate cleanup | Audit its separate removal workflow. Prefer sharing the guard, but explicitly decide whether this ships together; do not claim app-wide coverage if duplicates remain unguarded. |

### Implementation starting points

- `backend/services/plex.py` and `backend/services/emby_base.py`: native service adapters; the latter supplies shared Jellyfin/Emby behavior.
- `backend/jobs/candidate_file_ops.py`, `backend/tasks/cleanup.py`, and `backend/core/workflow_locks.py`: shared workflow, deletion/move routing, and concurrency boundaries.
- `backend/jobs/duplicate_file_ops.py`: separate duplicate-removal workflow to audit.
- `backend/services/candidate_lifecycle.py`, `backend/core/auto_delete.py`, and candidate UI/API types: distinguish temporary deferral from a canceled or newly postponed deadline.

Do not mistake `candidate_workflow_lock` for playback protection: it serializes Reclaimerr operations, not playback started in an external client. Select the guard placement after tracing collection-removal preparation as well as the final file operation.

### Acceptance checks

1. A playing movie, episode, or child of a season/series removal is retained; an unrelated item can still be processed.
2. Paused playback, shared files, multiple versions, multiple servers, and differing path prefixes are handled without protecting or deleting the wrong copy.
3. Empty sessions allow cleanup; timeouts, authentication failures, and invalid responses defer affected operations without being interpreted as “nobody watching.”
4. Scheduled, manual, moved, and approved-request candidates obey the same guard. Include duplicates if they are part of the chosen release scope.
5. Deferral preserves candidate history, deadline, collections, files, and monitoring settings. Playback ending permits a later attempt; stale snapshots refresh during long jobs.

## 2. Curated rule presets

### Problem and expected experience

The rule editor is powerful, but users must know which fields, scopes, conditions, and actions belong together. Import/export solves transporting a rule, not choosing a sensible starting point.

Maintainerr offers a shared community rule list. Reclaimerr's first version should use a small, bundled, reviewed catalog rather than requiring a new public service. A community marketplace can be considered separately later. [Maintainerr community rules](https://docs.maintainerr.info/api/rules/#community-rules)

Proposed flow: **Rules → Add from preset → select a preset → configure it in the existing editor → preview → save.** Selecting a preset must not create a rule or run cleanup by itself.

### Suggested initial catalog

These are proposed starting values, not user-approved retention policies. Validate the exact field names and semantics against the current engine when creating the definitions.

| Preset | Starting behavior | User configuration |
| --- | --- | --- |
| Old unwatched movies | Movie versions added more than 180 days ago with no recorded viewing | Library selection and age |
| Watched movies not revisited | Movies watched before, with the last watch more than 90 days ago | Library selection and age |
| Fully watched seasons | Seasons fully watched by selected users, with no recent playback for 30 days | Users, library, and age; explain incomplete/unknown history |
| Keep the newest two seasons | Match regular seasons older than the newest two, using the existing season-rank fields | Library and number of seasons to keep; explain the ranking basis |
| Large movies for review | Movie versions exceeding an editable file-size threshold | Library and size; automatic deletion stays off |

### Recommended first implementation

1. Store presets as versioned data with a stable ID, title, description, scope, prerequisites, and an ordinary rule definition. Keep them in one source of truth; use the existing rule validator instead of creating a second evaluator.
2. Add a compact picker showing what each preset matches and what the user needs to configure. Selecting one creates an unsaved editor draft with editable values.
3. Never embed installation-specific library, service, profile, or user IDs. Require users to select applicable values before saving; never silently substitute a different service or omit a condition when a prerequisite is missing.
4. Recommend saving new preset rules disabled, with automatic deletion off and no move behavior enabled. Keep preview and explicit action selection visible. Explain the distinction between enabling a rule to generate candidates and enabling automatic deletion.
5. Save an ordinary independent rule. Editing it or updating the bundled catalog must not overwrite the user's saved configuration. Existing import/export continues to work; no persistent preset subscription is needed.

An optional read-only catalog endpoint can serve the picker if presets live in the backend. Choose its exact interface after inspecting the current API conventions; avoid adding a database table unless an actual requirement needs one.

### Implementation starting points

- `frontend/src/routes/rules.svelte`: Add Rule, import/export, and existing rule list controls.
- `frontend/src/lib/components/settings/rules/advanced-rule-editor.svelte`: configure and preview a draft using the established editor.
- `backend/api/routes/rules.py`, `backend/models/cleanup.py`, and `backend/core/rule_engine.py`: create/import validation, normal rule payloads, and supported fields/operators.

### Acceptance checks

1. Every preset validates for its intended scope and produces expected matches against representative data, including missing watch history and specials.
2. Selecting or canceling a preset changes no saved rule and schedules no action. Saving preserves the chosen safe defaults.
3. Missing integrations, users, or libraries are explained; invalid drafts cannot silently broaden their matching criteria.
4. Editing and importing/exporting a saved preset rule behave exactly like other rules. Catalog updates leave existing rules untouched.
5. Test the picker/editor flow and backend validation. Confirm all preset descriptions agree with their actual conditions and actions.

## 3. Exclude media from one rule — discussion only

### Plain-language explanation

**Protection** means “keep this media even if cleanup rules match it.”

**An exclusion for one rule** means “ignore this rule for this media, but continue evaluating other rules.”

Example: a movie matches Rule A, **Unwatched for 180 days**, and Rule B, **File larger than 40 GiB**.

| Choice | What happens |
| --- | --- |
| Protect the movie | Cleanup is blocked by the protection, regardless of either matching rule. |
| Exclude the movie from Rule A | Rule A stops contributing. Rule B can still make the movie a candidate and determine its action. |
| Exclude the movie from Rule B | Rule A can still make it a candidate. |
| Exclude it from both rules | Neither of these rules makes it a candidate; another rule could still do so. |

This is useful for exceptions to a policy without changing the policy for everybody else. It is not a substitute for “keep this movie.” Maintainerr supports exclusions scoped to one collection/rule group as well as global exclusions. [Maintainerr exclusions](https://docs.maintainerr.info/collections/#excluding)

The earlier comparison used duplicate cleanup as an example. Reclaimerr's dedicated duplicate tool is a separate workflow, so do not assume a rule exclusion would change it. The two ordinary cleanup rules above are the clearer example.

### How this differs from existing automated protection

Reclaimerr already stores `ProtectedMedia.source_rule_id`. That identifies the rule that **created a protection**; it does not mean “excluded only from this rule.” Reusing that field for exclusions would confuse provenance with enforcement scope.

A separate exclusion would filter a particular rule's match before its action, instance selection, tags, and deletion delay are merged into the candidate. It would not modify the underlying rule definition or create a global protection.

### Important tradeoffs to discuss

1. Removing one matching rule can make the remaining action more destructive. For example, excluding an Unmonitor Only rule could leave a Delete rule as the winner. It can also shorten the delay when the excluded rule supplied the longest review period.
2. The UI must show the result of the exclusion: which other rules still match and what action remains. It must not imply the title is now safe from deletion.
3. Decide whether exclusions cover only an exact target or also descendants. A sensible proposal is that a series exclusion for a selected rule covers its seasons/episodes, while one episode's exclusion does not exempt its whole series.
4. Decide whether exclusions expire. A small first version could use permanent, manually removable exclusions; temporary protection already exists for users who simply need more time.
5. Decide how existing candidate deadlines change. Recommended starting point: never silently accelerate an existing deadline or escalate its action as an immediate side effect of adding an exclusion; show the impact and settle a reconciliation policy before coding.

### If the user chooses to proceed

Use a distinct rule-exclusion model with rule identity, media scope, creator, and timestamp. Define idempotent add/remove operations and admin permissions. Apply the same filtering to previews, scans, and execution-time reconciliation so a stale candidate cannot continue acting on an excluded rule. Preserve unrelated matches, global protections, and pending requests.

Inspect `backend/core/protection_scope.py`, `backend/database/models.py`, rule evaluation in `backend/tasks/cleanup.py`, and candidate lifecycle handling before deciding the schema. Existing scope helpers are useful references, but exclusion inheritance and protection overlap are not automatically the same operation.

Test two-rule conflicts, action escalation, deadline shortening, parent/child scopes, preview/scan agreement, stale candidates, and restoring eligibility after removing an exclusion. Decide what happens if the excluded rule is edited or deleted. Do not implement this feature until the user confirms that the distinction is useful to them.

## Next-chat starting instructions

1. Read this handoff and the current repository instructions. Recheck the checkout: at handoff time it includes user-staged release/version changes. Do not reset, overwrite, or commit those changes incidentally.
2. Playback protection is implemented. Curated rule presets are the recommended next feature; settle the catalog and editor behavior before coding.
3. Trace the relevant existing workflows and verify native provider API details with current primary documentation. Implement one feature at a time, with focused tests and a short changelog note when it is complete.
4. Leave collections per rule and poster overlays out of scope. Treat exclusions for individual rules as undecided until explicitly selected.

Useful verification commands are the existing targeted pytest suites, `ruff check`, `ruff format --check`, `basedpyright`, and frontend `npm run check` / `npm run build`. The previous session could not connect to an in-app browser; retry browser availability in the new session rather than assuming that limitation is permanent. No real media should be deleted to verify these features.
