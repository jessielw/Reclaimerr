# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - y-m-d

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
- Fixed failure when migrating DB (table _alembic_tmp_notification_settings already exists)

### Removed

-
