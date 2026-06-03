# Docker

Docker is the standard way to run Reclaimerr outside of development.

## Example Compose

```yaml
services:
  reclaimerr:
    image: ghcr.io/jessielw/reclaimerr:latest
    container_name: reclaimerr
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data
      - /media:/media
    ports:
      - "8000:8000"
```

## Common Environment Variables

```yaml
DATA_DIR=./data
API_HOST=0.0.0.0
API_PORT=8000
TZ=America/New_York
UMASK=022
PROXY_TRUSTED_HOSTS=127.0.0.1,::1
```

## Volume Guidance

- Mount the same library paths that your media server uses.
- Mount any destination path used for move-based cleanup.
- Keep the data directory persistent so the database and logs survive restarts.

## When You Are Behind A Proxy

If the API sits behind SWAG or another reverse proxy, make sure forwarded
headers are preserved and `PROXY_TRUSTED_HOSTS` points at the proxy IP or CIDR.
Set `Application URL` in General Settings if you want Plex and OIDC callbacks
to use a fixed public base URL.

See the [production guide](production.md) for the hardening checklist.
