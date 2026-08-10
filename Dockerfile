# syntax=docker/dockerfile:1.7

FROM node:20-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build:client

FROM node:20-bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash bubblewrap ca-certificates curl git python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt uv

COPY access_layer/ ./access_layer/
COPY backend/ ./backend/
COPY docs/ ./docs/
COPY README.md LICENSE ./
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist/
COPY docker/entrypoint.sh /usr/local/bin/k-agent

# Runtime state is always mounted here. The image contains no .env, credentials,
# sessions, Skills, Team databases, scheduled-task databases, or workspaces.
RUN chmod 0755 /usr/local/bin/k-agent \
    && mkdir -p /app/.k_agent \
    && chown -R node:node /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    K_AGENT_HOME=/app/.k_agent \
    HOST=0.0.0.0 \
    PORT=3001 \
    AGENT_BACKEND_HOST=127.0.0.1 \
    AGENT_BACKEND_PORT=3002 \
    AGENT_BACKEND_URL=http://127.0.0.1:3002 \
    RELOAD=false

USER node
EXPOSE 3001
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3001/api/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/k-agent"]
