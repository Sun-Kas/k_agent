# Project Instructions

- Keep `access_layer/` and `backend/` as independently running services.
- The Access Layer owns public APIs, sessions, persistence, and request concurrency.
- The Agent Backend owns stateless model and tool execution.
- Preserve the HTTP-only boundary between the frontend and backend services.
- Add concise comments for non-obvious architecture, security, concurrency, and lifecycle decisions.
- Run focused backend tests and frontend checks after changing behavior.
- Never commit `.env` or `.k_agent/` again (one-time snapshot already in git history; keep them gitignored).
