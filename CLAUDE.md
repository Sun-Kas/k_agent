# Project Instructions

- Keep `access_layer/` and `backend/` as independently running services.
- Access Layer must not import `backend` (and Agent Backend must not import `access_layer`). They talk over HTTP.
- The Access Layer owns public APIs, sessions, persistence, and request concurrency.
- The Agent Backend owns stateless model and tool execution.
- Preserve the HTTP-only boundary between the frontend and backend services.
- Add concise comments for non-obvious architecture, security, concurrency, and lifecycle decisions.
- Run focused backend tests and frontend checks after changing behavior.
- Never commit `.env` or `.k_agent/` again (one-time snapshot already in git history; keep them gitignored).

- Follow `AGENTS.md`: Web and the standalone K-Agent CLI terminal page must evolve together in functionality, visible information, state and behavior. Update the parity checklist and validate both clients for frontend changes.
- Explicit parity exception: CLI does not need speech input, dictation, continuous voice input, microphone access, speech readout, or a desktop pet. Preserve Web speech and desktop-pet features; do not reintroduce a CLI microphone, browser input bridge, speech output, or terminal pet.
