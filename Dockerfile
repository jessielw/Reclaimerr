# syntax=docker/dockerfile:1.7
# cSpell:disable

## frontend build stage
FROM node:25-alpine3.22 AS frontend-build
WORKDIR /frontend
COPY frontend/package.json ./package.json
RUN npm install
COPY frontend ./
RUN npm run build

## backend base image
FROM python:3.13-slim AS backend-base
ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1 \
	FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
RUN apt-get update \
	&& apt-get install -y --no-install-recommends build-essential curl \
	&& rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN python -m pip install --upgrade pip \
	&& python -m pip install .
RUN mkdir -p /app/data/database /app/data/logs /app/data/static/avatars
EXPOSE 8000

## API image (includes frontend build)
FROM backend-base AS api
COPY --from=frontend-build /frontend/dist /app/frontend/dist
CMD ["sh", "-c", \
	"granian --interface asgi --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8000} backend.api.main:app"]
