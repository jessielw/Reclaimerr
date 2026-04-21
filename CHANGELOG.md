# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0-beta.11] - 2026-04-20

### Fixed

- Plex was not parsing all data correctly across all users/history
  - This will slightly increase the sync time
- Never watched false positives

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
