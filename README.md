<p align="center">
    <img height="100px" alt="Reclaimerr's logo with name" src="public/logo_name.svg" />
</p>

<p align="center">
<picture><img alt="Desktop Build" src="https://img.shields.io/github/actions/workflow/status/jessielw/reclaimerr/.github%2Fworkflows%2Fdesktop.yml?style=flat&logo=github&logoColor=white&label=Desktop%20Build"></picture>
<picture><img alt="Docker Build" src="https://img.shields.io/github/actions/workflow/status/jessielw/reclaimerr/.github%2Fworkflows%2Fdocker.yml?style=flat&logo=docker&logoColor=white&label=Docker%20Build"></picture>
<picture><img alt="Frontend Check" src="https://img.shields.io/github/actions/workflow/status/jessielw/reclaimerr/.github%2Fworkflows%2Ffrontend.yml?style=flat&logo=svelte&logoColor=white&label=Frontend"></picture>
<picture><img alt="Backend Check" src="https://img.shields.io/github/actions/workflow/status/jessielw/reclaimerr/.github%2Fworkflows%2Fruff.yml?style=flat&logo=python&logoColor=white&label=Backend"></picture>
</p>

**Reclaimerr** scans media libraries for eligible items, tracks protection and
deletion requests, and routes the final action through the appropriate service.

- [Getting started](docs/getting-started/index.md)
- [Docker deployment](docs/deployment/docker.md)
- [Contributing](docs/development/contributing.md)

## Capabilities

- Supports Jellyfin, Plex, and Emby
- Integrates with Radarr and Sonarr when configured
- Scans candidates using your reclaim rules
- Respects protection, pending requests, and approval flows
- Supports scheduled tasks, including automatic cleanup deletion
- Can move instead of delete when configured

## Quick Start

1. Install Reclaimerr with Docker, Desktop, or source.
2. Connect at least one media server.
3. Set one server as the main server when multiple servers are configured.
4. Review the task schedule before enabling deletion.

## Support

- [Documentation](https://jessielw.github.io/Reclaimerr/)
- [GitHub Discussions](https://github.com/jessielw/Reclaimerr/discussions)
- Matrix: https://matrix.to/#/#reclaimerr:matrix.org

## Preview

![image](public/login.png)
![image](public/dashboard.png)
![image](public/movies.png)
![image](public/series.png)
![image](public/reclaim-candidates.png)
![image](public/rules.png)
![image](public/rule-node.png)
![image](public/settings-notifications.png)
![image](public/settings-servers.png)
![image](public/settings-tasks.png)
![image](public/settings-users.png)
![image](public/user-signals.png)
