# syntax=docker/dockerfile:1.7
# cSpell:disable

## frontend build stage
FROM node:25-alpine3.22 AS frontend-build
WORKDIR /frontend
ARG VITE_APP_CHANNEL=dev
COPY frontend/package.json ./package.json
RUN npm install
COPY frontend ./
RUN VITE_APP_CHANNEL=${VITE_APP_CHANNEL} npm run build

## backend base image
FROM python:3.13-slim AS backend-base
ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1 \
	FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
RUN apt-get update \
	&& DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends build-essential curl gosu tzdata \
	&& rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md CHANGELOG.md ./
# install the exact versions from uv.lock so new upstream releases can't break the image
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
	&& uv pip install --system --no-cache -r /tmp/requirements.txt \
	&& rm /tmp/requirements.txt
COPY backend ./backend
COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN uv pip install --system --no-cache --no-deps .
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
	&& mkdir -p /app/data/database /app/data/logs /app/data/static/avatars
EXPOSE 8000

## API image (includes frontend build)
FROM backend-base AS api
COPY --from=frontend-build /frontend/dist /app/frontend/dist
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh", "-c", \
	"exec granian --interface asgi --workers 1 --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8000} backend.api.main:app"]
