# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add documentation link to about page

## [0.1.0-beta.28] - 2026-06-03

### Added

- Add **opt-in** automatic deletion task
  - Must be enabled in **Settings -> General** and in the **Tasks** page
- Ability to de-select arr instance in rule editor
- Add **Application URL** in **Settings -> General** for Plex and OIDC callback URL generation behind reverse proxies

### Changed

- Improve path resolution between arrs and Reclaimerr
- Update operator labels for clarity and enhance rule validation tests (existing rules don't need anything adjusted)
  - contains_any -> matches any
  - not_contains_any -> matches none
  - contains_all -> matches all
  - not_contains_all -> does not match all

### Fixed

- Filename regex matching would throw an error when there was no media to run against when creating a rule
- Plex and OIDC auth redirects now use the configured **Application URL** when present, preventing Plex login from landing on `localhost:3000` or an HTTP callback behind strict Docker/Granian reverse proxies
- Plex login button not being enabled on initial launch of application even if configured
- Issue sometimes with the UI hanging on certain browsers

## [0.1.0-beta.27] - 2026-06-01

### Added

- Added Plex 'favorite' handling
  - Plex favorites are actually handled via **watchlists**
  - Plex users **must** authenticate via Plex with Reclaimerr
- Rule preview now shows the count of active, favorited, and protected media to give the user more information on why their rule might not be showing a specific result

### Changed

- Rule `Arr tags` rule operator has been tightened up to only contain operators that make sense for it
- Improved tag detection for the arrs

### Fixed

- Rule `Arr tags` could be inaccurate when running rules against them
- Dashboard would show media count that had been soft deleted

## [0.1.0-beta.26] - 2026-05-30

### Added

- Rules
  - Movie collections (TMDb) rule
    - New inline collections rule picker so users can see what Collections are available, but they are also free to type the collections
      - API endpoint to parse collections from the backend
  - Added operators **contains all** and **does not contain all** for list based rules
- Collection name for movie in candidates and info

### Changed

- Improved error handling and logging substantially for all HTTP calls
- Settings > System is now organized alphabetically
- Sync now collections TMDb collection data

### Fixed

- Rule node editor could get slightly cramped on some screens
- Issue with Plex authentication

## [0.1.0-beta.25] - 2026-05-29

### Added

- Rule
  - **Seerr requester has watched**
    - Makes it possible for deletions when movies/episodes are watched by the requester so they can automatically be cleaned up after
  - **Season watch percentage**
  - **Season fully watched**
- Added new settings tab **User Signals**
  - Built inline compact favorites viewer to browse what titles your users have favorited
  - Can now map Seerr users to their media server usernames via a new mapping component
- Added the ability to support **Leaving Soon**
  - Works on Jellyfin, Emby, and Plex
    - Plex displays the titles in the collection based on the library where as the others put it in a single place
  - Automatically refreshes this during candidate syncs
  - Allows renaming the base (keeps track of it to update/clean up the old name)
  - Cleans up the collection when Leaving Soon is disabled
- Added session management
  - Added new house keeping task to trim old sessions
- Added generic OIDC support
  - Button in the login screen that will show up if it's been enabled by the administrator
  - Added new settings tab to setup OIDC
- Added media-server account sign-in and identity linking
  - Added login-time authentication for **Jellyfin/Emby** (username/password)
  - Added login-time authentication for **Plex** via PIN redirect/callback flow
  - Added persistent `media_user_identities` storage to track source identities per media server
  - Added automatic account resolution/linking at sign-in (existing link, email match, case-insensitive username match, or create user)
  - Added admin notice generation when a media identity matches multiple local users and requires manual linking
  - Added admin APIs for media identity management
    - List identities
    - Link identity to a local user
    - Unlink identity from a local user
  - Added media identity linking controls to **Settings -> Users**
  - Added media auth provider discovery endpoint for the login screen

### Changed

- Updated dep FastAPI
- Each media server now keeps a snapshot for who watched what
  - Snapshots are now gathered during syncs and as needed
- Rule validation now enforces field compatibility for the selected `target_scope` across create, preview, import, and update
- Rule editor field picker is now filtered by target scope (Movie Version, Series, Season, Episode) and shows incompatible legacy conditions inline
- Reworked login UI to use a compact method switcher instead of stacked auth sections
  - Local / Media / SSO modes now render one active flow at a time
  - Media provider selector now uses service SVGs and cleaner labels
- Updated account password behavior for media-auth users without local passwords
  - Users without a local password can set one without entering a current password
- Updated dependency Granian to v2.7.5
- Updated dependency apprise to v1.11.0

### Fixed

- Candidates and Protected mobile view had some visual bugs
- Rebuilt media detail modal to look much nicer on mobile
- Delete button in notification on mobile spilling over
- Log level validation from env could still sometimes not be set
- `watch.never_watched` advanced rules now evaluate correctly for Series and Season targets (including stale watch timestamps after re-adds)
- Invalid field/target-scope rule combinations no longer fail silently; API now returns clear 422 validation errors
- Path rule validation is now operator-aware for both `media.path` and `media.file_name`; literal operators no longer get treated as regex, and invalid scope criteria are pruned correctly in the rule editor
- `media.file_name` rule matching now falls back to path basename when explicit filename metadata is missing, so AND combinations like folder path + filename regex evaluate correctly
- `media.path` literal operators now treat folder values as path prefixes (not just exact file-path matches), and the path browser is only shown for regex mode to avoid operator/value mismatches
- Spacing between label/input in pattern picker for Path
- Path mapping for deletions/multi arr
  - Added mapped path comparison helpers for media-server paths vs Arr root paths
  - Movie version deletion now promotes to Radarr only when the selected version set covers the full Radarr movie entry
  - Multi-Radarr ambiguity now fails closed into media-server fallback instead of broad Arr deletion
  - Season and episode deletion now try all Sonarr refs ordered by path match, then fall back to the media server when enabled
  - Episode "not found in Sonarr" no longer hard skips when media-server fallback can delete it
- Logging in via email on local auth would fail

## [0.1.0-beta.24] - 2026-05-23

### Added

- IMDb ratings
  - IMDb vote count & rating to the rule engine
  - New task to refresh IMDb ratings daily (can be disabled if the user does not want IMDb)
    - This task daily will grab IMDb official rating data sets and update the database with the current ratings (these data sets are only refreshed once every 24 hours so running this task more than that is fruitless). There are guards that check the hash/304 not modified header that will prevent running if nothing has changed
- AniList anime ratings
  - Vote count, popularity, and favorites to the rule engine
  - New task to refresh AniList ratings daily (can be disabled if the user does not want AniList)
- Candidates view now shows date added metadata

### Changed

- Now shows IMDb rating data on candidates, protected pages, and movie/series information modals
- Now shows AniList rating data on candidates, protected pages, and movie/series information modals
- Border of posters have been themed based on media type
- Slightly increased the size for some of the posters
- Improved how candidates are scoped at the episode level, will now group if all episodes in a season or all seasons in a series are selected in the bulk selector to protect
- Bulk selector modal is now blocked during processing until it's completed

### Fixed

- Log level not being passed through from env
- Arr instance selector in mobile was too large
- Arr instance in mobile buttons was not centered vertically
- Bug protecting episode scoped candidates

### Removed

- Media type badge from Protected

## [0.1.0-beta.23] - 2026-05-21

### Added

- Support for Seerr requested by username based rules
  - During rule preview this will be accurate and cached TTL for 5 minutes, during live candidate scans this will be grabbed from Seerr every single run
  - If you have a rule utilizing this feature and it fails there will be a notice in the UI alerting you that there is an issue communicating with Seerr and the rule will be disregarded until this is resolved
  - Users are parsed upon opening a rule (if Seerr is enabled) and will be displayed via a "Seerr User Picker" modal
    - Internally the user IDs from Seerr are utilized but visually you'll be able to see them by name (this was needed in case users names changed on Seerr)
- New house keeping task to clean up admin notices older than 90 days automatically (read or unread)
- Option in **Settings -> General** to add exclusion to the **\*arr** that is enabled by default
  - With this option on Reclaimerr will now add to the exclusion list for the arr automatically to prevent lists/syncs automatically re-grabbing that title
- Support ignore **Jellyfin/Emby** user favorites _(currently this feature isn't supported on Plex)_
  - During preview user favorites are snapshot every 10 minutes to avoid hitting the server over and over again while users are testing rules but they will be up to date always during live candidate scans
  - Added a setting in **Settings -> General** to enable/disable this feature and a box to add the usernames you'd like to automatically ignore their favorites
    - Supports copy/paste for specific users or all users and displays which users currently has favorites

### Changed

- Rebuilt the admin notice system
  - Can now mark read/unread on notices
  - Notices can have context/links etc

### Fixed

- Side bar light mode text was barely visible for notices panel
- Radarr deletion logic when media deletion is disabled and the user is deleting a movie version. _(Reclaimerr now checks whether a version-scoped movie candidate actually represents the full Radarr movie entry. If it does, for example the movie only has one known version, Reclaimerr safely promotes it to a normal Radarr delete. If it is truly partial, for example one version selected but another version still exists, Reclaimerr will not use Radarr because that could delete files the user did not select)_

### Removed

- System alert banner _(this can be handled via the notices panel)_

## [0.1.0-beta.22] - 2026-05-19

### Added

- File operation queue system
  - Before when you moved/deleted a file it would be a **blocking** process - which means you'd have to wait until it was complete right on your screen. Now we queue moves/deletes to be done and keeps track of them providing some high level progress of what is being done
- Add history page - displays reclaimed media and clean up activities with filtering options
  - Add history page to sidebar (nav)
- Close button to all toasts
- Select all button in Candidates route for current page
- Requests now shows a notification dot if there are pending requests

### Changed

- Improve handling of jellyfin/emby playback reporting plugin data retrieval
- Improve grabbing episode data from all users on Emby/Jellyfin
- Improved error handling for testing notifications
- History has been moved to it's own dedicated route
- Improve deletion request handling
- Enhance user feedback for approved deletion requests
- Improved access control on navbar
- Rule editor keys are now grouped and organized in ABC order where needed for easier navigation
- Optimistically backfill Candidates as they are moved/protected/deleted if available
- Substantially enhanced the speed/efficiency of checking of status bar updates for notifications
- Updated numerous backend dependencies (alembic, apprise, argon2-cffi, cryptography, pillow, pydantic-settings, pyjwt, python-dotenv, sqlalchemy[asyncio], and tenacity)

### Fixed

- Candidates overflowing on mobile with long rule names
- Fix mobile menus not wrapping properly in Candidates route
- Fix bulk select buttons not wrapping properly on mobile in Candidates route
- About, Account, General, Notifications, Tasks, and Rule loading spinners was not consistent with the rest of the app
- Fix on about not showing header during loading like the other routes

### Removed

- Removed history from Settings
- Removed history from Candidates

## [0.1.0-beta.21] - 2026-05-17

### Added

- New task to check for updates (defaults to running once per hour - can also be disabled completely if desired)
  - Option to disable **optional** tasks
- New **Notices** modal that can be accessed via the bell icon on the sidebar
  - Will show up green if there is a notice (right now this is only hooked up to new updates)
- Added new api endpoint `info/update-status`
- General settings **\*Arr Default Behavior** options to either delete/unmonitor when deleting/moving media that is not a candidate + falls back for all other rules
- Dashboard now shows total size of movies/series on the 'Total' cards
- Added a **Auto** option to the dashboard size formatter - this intelligently selects the most appropriate size to show
- Now shows episode names when utilizing the scope **Episodes** in the rule previewer
- Env variable `LOG_RETENTION_DAYS` _(defaults to 30)_ to keep N daily rotated log files
- Candidates unified view with filters for movie/series
- Added a notification beside **Candidates** on the nav bar that will now be displayed if there is **any** candidates at all
- Can now clone rules
- Now remembers which tab was last used in Requests
- Now shows a 'DEV' badge when in dev on the top right hand corner of the screen

### Changed

- Add ability in the API to enforce enable/disabling tasks
- Rule preview system now sorts the results in an ABC-123-S01 order
- Improved alembic revisions internally
- Improved error handling of invalid season/episode combinations
- Move is only enabled for Series, Seasons, or Movie Versions (not episodes)
- **Series** cards now show episode count instead of season count (hovering the number will display the season/episode count in a tool tip)
- Candidates API now supports 'all'
- Poster sizes for candidates are all now the same and slightly larger
- Requests search/filter section now matches the rest of the program
- Media servers and services **Test** button now shows a green check instead of a toast on success
- All toast messages will now be colored based on their type (success, error, warning, etc.)

### Fixed

- Sometimes media could be left behind during a deletion request for episodes
- Several bugs for episode level protection/deletions
- Issue when calculating space in candidate views
- Issue when sorting candidates via row for Series - they are now grouped a lot more cleanly

## [0.1.0-beta.20] - 2026-05-15

### Changed

- Improve upsert on episodes to avoid any unique key constraints
- Only show count badge for movies if there is more than 1 movie version
- Only show count badge for series if there is more than 0 seasons

### Fixed

- Added_at field not being updated/valid during syncs/deletes of media
- Enhance media server removal logic with fallback checks in candidate deletion for sonarr/series
- During move the file would be deleted by the **\*arrs** BEFORE Reclaimerr could move it safely - now Reclaimerr moves the file, un-monitors/deletes (if enabled) and refreshes the arr so it has the updated information

## [0.1.0-beta.19] - 2026-05-14

### Changed

- Update macOS build configurations for ARM and Intel architectures (desktop)

### Fixed

- Now utilizes rescan instead of refresh for the **\*arrs**, this fixes an issue where when deletions happened it was not updated in the arr right away

## [0.1.0-beta.18] - 2026-05-14

### Added

- Added episode granularity
- Series candidates view now has collapsible season and episode level control
- New Rules
  - Series status (ended, continuing, etc)
  - **\*arr** monitored status
  - Free disk space
  - Seerr requested
- Now shows season counts and movie version counts on posters for movies/series tabs

### Changed

- Improved pulling from Tautulli and Jellyfin/Emby play back reporting plugin
- Substantially improved notification output with some customization on what is returned
  - Can now control a standard/compact message
  - Can now send N number of candidates found with a total of reclaimable space
  - New notifications are enabled by default on creation now

### Fixed

- Last viewed was in-accurate if a file was removed and re-added to a media server
- Fix wrapping issues on medium size screens for notification selection

## [0.1.0-beta.17] - 2026-05-11

### Added

- Can now initiate control **Candidate Sync Task** from the rules page
- Added option in General settings to enable/disable media server deletion/move fallback (enabled by default)
- Can now select **\*arr** action
  - Can delete completely
  - Can Unmonitor + Delete (i.e. removes the file but keeps the record in the arr unmonitored)

### Changed

- Add +2 pixels to top margin for all routes in mobile
- Nav bar customizations are now saved per user at the browser level (in case multiple users was to use the same browser)
- Improve arr instance handing and set tag to Disabled by default

### Fixed

- Inconsistent page widths/alignment on larger view ports
- Inconsistent vertical spacing throughout the pages
- Deletes via media server instead of arr after multi arr update
- Very rare edge case on determining the correct server during cleanup (wouldn't have actually caused any bugs on deletion)
- Nav bar customization not saved on refreshes

## [0.1.0-beta.16] - 2026-05-10

### Added

- In docker you can now pass in variables for timezone, PUID, PGID, and UMASK if desired
- New rules
  - TMDB release date
  - TMDB last air date
  - Season air date
  - Days since released
  - Days since first aired
  - Days since last aired
- Can now click the Reclaimerr logo/name to navigate to Dashboard
- Added a customizable menu (opened via the menu icon) for the side bar to hide unused tabs

### Changed

- arr instances are now enabled by default when creating new instances
- Now shows all arr instances even disabled ones
- TMDB fire icon is now filled/style a little better
- Re-position buttons in candidates view for move/delete
- Candidates view tables have been reworked to show more data at a glance
- Rules now lives in the sidebar instead of settings
- Reworked side bar visuals to be a bit more space efficient
- Updated niquests

### Fixed

- Sync issue that could happen in edge cases when a user still had old rules/candidates before the rule engine rework
- Edge case bug where count assignment would overwrite the version candidates count in very rare cases
- In some setups it was possible for Radarr to still monitor deleted movies that was a candidate from Reclaimerr
- TMDB section not visible in light mode
- Move button not visible in light mode for bulk select in candidates view
- Fixed candidates not showing the properly calculated resolution in some cases

## [0.1.0-beta.15] - 2026-05-08

### Fixed

- Multiple arr instances not showing on dashboard (UI bug only)
- Tautulli not enabling without a service restart

## [0.1.0-beta.14] - 2026-05-08

### Added

- Linked media sync now refreshes supplemental matches for movies, series, and seasons using stable path tails.
- Tautulli can now update movie, series, and season stats through mapped Plex IDs when Plex is a linked server instead of the main server
- Jellyfin/Emby Playback Reporting can now update movie, series, and season stats through mapped IDs when Jellyfin/Emby is linked instead of main

## [0.1.0-beta.15] - 2026-05-08

### Fixed

- Multiple arr instances not showing on dashboard (UI bug only)
- Tautulli not enabling without a service restart

## [0.1.0-beta.14] - 2026-05-08

### Added

- Now parses detailed mediainfo from media servers for movies and lightly gets what makes sense for series
- Candidates
  - Movie/Series & Season level media info (movies are more detailed)
  - Movie version control of deletions
  - Added TMDb section for a quick view of stats/link to the media (series will show current status as well)
- Multi \*arr multi instance
  - Added ability to name \*arr instance in frontend
  - Added ability to manage \*arr instances via API in backend
  - Support in ServiceManager
- Rule Engine
  - Rebuilt rule engine completely
  - Added support for complex logical rules via AND/OR and groups
  - Can now preview potential candidates based on the current rules selections
- Added rule importing/exporting to share rules with the new rule engine
- Now keeps history of reclaimed media (starting from **0.1.0-beta-14**)
- Dashboard
  - Reclaim history
    - Total space reclaimed
    - Total movies reclaimed
    - Total series reclaimed
- Added a section to **Candidates** API to view reclaimed history in a paginated format with filters/search
- Added path mapping toggle to General settings
- Added path mapping destination for movies & series
- Added api endpoint **/candidates/move** on the backend
- Added option to enable to move files instead of deleting in General setting
- Added move button that is visible _(when the option is enabled to move instead of delete)_ in settings to movies/series/seasons
- Added backend support to handle moving files
- Now cleans up related files and parent folder when deleting files where it makes sense (behaves similar to Sonarr/Radarr)
- Added \*arr tag rules
- **Jellyfin/Emby** will now pull watch data from the **Playback Reporting Plugin** if it's enabled/available for supplemental data
- Now supports **Tautulli** to supplement **Plex's** playback data
  - Added support in frontend to add it as service. If it's enabled it will automatically be used during all sync processes
- Desktop
  - Added a **Shutdown** button that will work for desktop mode in the **Settings -> General**. This allows users to easily shut Reclaimerr down if they lack a system tray _(This will not be functional for anything other than desktop)_
  - Now writes a **process ID** file _(reclaimerr.pid)_ in the **data** directory so users can kill this easily via scripting/terminal if needed
- Added useful information for each movie version/season that is requested to be protected in **Protected** requests for users such as filename, resolution, video codec, hdr flags, and file size
- Can now choose which version of files you'd like to protect for movies with multiple versions
- Added api/frontend controls for delete requests for users
- Add support for generic post action webhooks (Autopulse, etc.)
  - Added API support
  - Added frontend controls per arr instance (or all) in General Settings
- About
  - Added changelog viewer to break down release notes per version
  - Added a support section

### Changed

- When using Docker, see the compose example at `docker\compose.example.yml`. Add the media bind mount so Reclaimerr can remove extra files when items are deleted via your media server. Map the bind mount to the same path(s) your main media server uses to read media files; if you have multiple mounts, add them all and update the mappings in General Settings.
- Greatly improved resolution detection for ingested movies
- Upgraded niquests to 3.18.7
- Auto tagging configuration/tag suffix in general settings is now moved to per rule
- All switch elements should now use cursor-pointer
- Polished **Candidates** tab a little bit
- Candidates api endpoint **/candidates** now returns TMDB data
- Normalize all paths to be forward slashes internally when parsing from media servers
- Now parses season and episode paths on from the **main media server** _(episode granularity is still not in this update - but this is needed to ensure we're deleting/moving all the correct files)_
- Improved normalization of paths
- Improved style of action buttons for candidates
- Lock name for all services other than Sonarr/Radarr since we only will only have one instance for now
- Entire frontend now shows cleaner formatted file sizes (i.e. MB, GB, etc.)
- Info modal now sizes a bit better, cleaned up icons for buttons, changed 'Request' to 'Protect', and added 'Delete' request with hover tool tips
- Built an efficient fingerprint system to keep file signatures the same during name changes
- Movies & Series cards are now improved
  - Badges scale with the cards
  - Drop down menu to select requests/deletes quickly
  - Reworked lower buttons

### Removed

- \*Arr tagging in general settings (this is handled per rule now)

### Fixed

- Migration issue that could happen when jumping several version at once
- Arr tag requirements are now correctly set (A-Za-z0-9-) with a max of 50 that includes **-rec** for internal uses
- Media being parsed from other libraries that wasn't selected in the rule
- In rare cases posters could stretch
- Now deletes/un-monitors each season/series as all entries are removed
- Numerous loading spinners was not styled properly
- Username section in the side bar was not aligned properly
- Button not styled properly for Cancel Request in protection request
- A few nav items wasn't being styled for the full width correctly when selected

## [0.1.0-beta.13] - 2026-04-25

### Fixed

- Migration issue that could happen when jumping several version at once

## [0.1.0-beta.12] - 2026-04-24

### Added

- Rule Form
  - Path criteria for rules with optional regex filtering (@jeaboswell)
  - Series status to TMDB criteria for rules (@jeaboswell)
  - Notation about rule **Include Never Watched** stating how it can be unreliable when utilize Plex as a data point
  - Warning when a user sets up a rule that is only utilizing **Include Never Watched** that requires confirmation to continue
- Sonarr/Radarr
  - Retry on their GET routes (API lacks pagination for some reason)
  - Can now set timeout length from 30 - 3600 seconds to tune the length before it'll time out (in case of large libraries/slow systems/storage)

### Changed

- Unified Emby/Jellyfin to reduce duplicate code
- Refactor media sync for Emby/Jellyfin to use series creation date instead of earliest episode (@jeaboswell)
- Enhance media server deletion logic for cleanup candidates
- Updated FastAPI to v0.136.1
- Docker
  - Updated Granian to v2.7.4
- Loading spinners are now themed as the primary color

### Fixed

- Emby deletion fallback on cleanup
- Edge case on movie ingestion related to IMDb ID
- Fixed loading spinner in Rules page on loading (in production this will almost never be seen anyways)
- Logic related to never watched causing some candidates not to load up that should have

## [0.1.0-beta.11] - 2026-04-20

### Fixed

- Plex was not parsing all data correctly across all users/history
  - This will slightly increase the sync time
- Never watched false positives
- Test/Save button spacing on Radarr/Sonarr/Seerr settings pages

## [0.1.0-beta10] - 2026-04-19

### Added

- UserDataLastPlayedDate and UserDataPlayCount to emby api calls to improve accuracy (@phendryx)

### Changed

- Improve health check to prevent any potential false positives for Plex

### Fixed

- Ability to change host/port in the Docker release

## [0.1.0-beta9] - 2026-04-18

### Added

- Added support for Emby
- Dashboard
  - Added service url visible for each enabled service
  - Added icons for each available service

### Changed

- Dashboard
  - Service section in Dashboard will now only show services that are active
  - Services in Dashboard are properly capitalized
  - Now hides services that aren't enabled

## [0.1.0-beta8] - 2026-04-17

### Added

- Added the ability for desktop users to set **API_HOST, API_PORT, ADMIN_PASSWORD, and CORS_ORIGINS** via a **.env** file that is placed directly beside the executable
- Can now reset the **admin** password via `ADMIN_PASSWORD` env variable

### Changed

- Enhance Radarr and Sonarr ID resolution for movie and series candidates during tagging (if not yet cached from full scan)
- Improved accuracy of date time sort order for candidates
- Can now scan Plex items that are lacking TMDB data (usually due to legacy agents on Plex)

### Fixed

- A bug on desktop release showing wrong version in the logger
- Sidebar menu item not automatically scrolling out of the way when navigating on certain routes
- Plex's watch data was only being pulled from the user the token was generated from, this is now fixed and will display accurate watch history across all users (a full scan is needed to be ran to see these changes right away or wait for the scheduled run)
- Never watch filter returning false positives

## [0.1.0-beta7] - 2026-04-16

### Changed

- Improved false positives (for candidates where `added_at` is unknown they will now be skipped)
- Improved word wrap on the Reason column in candidates

### Fixed

- Add safe timestamp conversion method for invalid/out of range timestamps from Plex on Windows
- Plex false positives for **Never watched** and new items bypassing age filter
- Alignment of fields in rule form
- Failure on duplicate movie versions for initial ingestion

## [0.1.0-beta6] - 2026-04-15

### Changed

- Added a guardrail to prevent migration issues

### Fixed

- UI on initial setup page still implying there was strict password requirements

## [0.1.0-beta5] - 2026-04-15

### Added

- Alert system
- Now alerts when users switch their main server and they have stale library IDs for existing rules
- Resets "stale" jobs that was left over during a restart/shutdown during a running task

### Changed

- Improve Seerr API permission errors and log warnings a bit better
- Ensure old main server stays enabled during main server swap
- Relaxed password complexity, we'll let users be responsible for the complexity of their passwords
  - The only requirements are a min length of 3 and a max length of 64
- Increase time out for long running api calls for Plex, Jellyfin, Radarr, Seerr, and Sonarr

### Fixed

- Stale server entries after a main server swap
- Rules would not match with library IDs after a main server swap
- Added live lookup from Radarr/Sonarr if missing cached \*arr data (sync hasn't been ran since they was added)
- Don't scan library path after deletion if using media server to delete media directly
- Scan erroring out when invalid TMDB is parsed from a media server
- Fixed failure when migrating DB (table \_alembic_tmp_notification_settings already exists)
