#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI not found. Install Docker Desktop, OrbStack, or another Docker-compatible runtime first." >&2
  exit 127
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running." >&2
  exit 1
fi

tag="${1:-k-agent:snapshot}"
echo "WARNING: $tag will contain .env credentials and all .k_agent runtime data." >&2
echo "Do not push this image to any registry." >&2
docker build --file Dockerfile.snapshot --tag "$tag" .
echo "Built $tag"
echo "Run: docker run --rm --name k-agent-snapshot -p 127.0.0.1:3001:3001 $tag"
