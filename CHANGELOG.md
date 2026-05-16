# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- New task to check for updates (defaults to running once per hour - can also be disabled completely if desired)
  - Option to disable **optional** tasks
- New **Notices** modal that can be accessed via the bell icon on the sidebar
  - Will show up green if there is a notice (right now this is only hooked up to new updates)
- Added new api endpoint `info/update-status`

### Changed

- Add ability in the API to enforce enable/disabling tasks
- Rule preview system now sorts the results in an ABC-123-S01 order
- Improved alembic revisions internally

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
