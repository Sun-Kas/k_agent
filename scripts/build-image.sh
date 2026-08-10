#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI not found. Install Docker Desktop, OrbStack, or Colima first." >&2
  exit 127
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running." >&2
  exit 1
fi

tag="${1:-k-agent:local}"
docker build --file Dockerfile --tag "$tag" .
echo "Built $tag"
echo "Run with Compose: docker compose up -d"
