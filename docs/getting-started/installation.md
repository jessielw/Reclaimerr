# Installation

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- `uv` for source installs

## Docker

Docker is the production deployment path. See the
[Docker deployment guide](../deployment/docker.md) for compose examples and
volume guidance.

## Desktop

Desktop builds are available from project releases. They launch the backend and
start the UI locally.

## Source

For development or local testing:

```bash
git clone https://github.com/jessielw/Reclaimerr.git
cd Reclaimerr
uv sync
cd frontend
npm install
cd ..
uv run uvicorn --reload --reload-dir backend backend.api.main:app
```

Then start the frontend in a second terminal:

```bash
cd frontend
npm run dev
```

## Default Ports

- Backend: `8000`
- Frontend dev server: `3000`

