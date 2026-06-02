# Reclaimerr Docs

Reclaimerr scans media libraries for eligible items, tracks protection and
deletion requests, and routes the final action through the appropriate service.

Documentation for setup, usage, deployment, and development.

## Sections

- [Getting Started](getting-started/index.md) - install Reclaimerr and complete
  initial setup.
- [Features](features.md) - review the core workflow and supported services.
- [Using Reclaimerr](usage/how-it-works.md) - candidates, requests, and
  deletion flow.
- [Deployment](deployment/docker.md) - run Reclaimerr in Docker or behind a
  proxy.

## Capabilities

- Supports Jellyfin, Plex, and Emby
- Integrates with Radarr and Sonarr when configured
- Scans candidates using your reclaim rules
- Respects protection, pending requests, and approval flows
- Supports scheduled tasks, including automatic cleanup deletion
- Can move instead of delete when configured

## Project Links

- [README](https://github.com/jessielw/Reclaimerr/blob/main/README.md)
- [Features](features.md)
- [API Reference](reference/api.md)
- [Changelog](reference/changelog.md)
- [Contributing](development/contributing.md)
- [Architecture](development/architecture.md)
- [Rules](usage/rules.md)
- [Backups](deployment/backups.md)
- [SWAG reverse proxy example](deployment/swag.md)
- [Production deployment](deployment/production.md)
