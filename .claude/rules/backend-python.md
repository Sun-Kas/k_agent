---
paths:
  - "backend/*.py"
  - "backend/**/*.py"
  - "access_layer/*.py"
  - "access_layer/**/*.py"
---

# Backend Python Rules

- Prefer asynchronous I/O for request and streaming paths.
- Do not hold process-local locks across unrelated sessions.
- Release streaming resources and concurrency guards in `finally` blocks.
- Validate data received at service and archive boundaries.
- Keep blocking filesystem or SDK operations off the event loop.
