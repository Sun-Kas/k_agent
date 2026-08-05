"""Provider-neutral outbound-network policy resolution."""

from __future__ import annotations

from backend.runners.base import RunnerContext


def network_access_enabled(ctx: RunnerContext) -> bool:
    """Resolve a run override before falling back to the service default.

    Only real booleans are accepted. Treating strings such as ``"false"`` as
    truthy would silently broaden a sandbox boundary at the HTTP seam.
    """

    override = ctx.options.get("networkAccess")
    if isinstance(override, bool):
        return override
    if ctx.settings is not None:
        return ctx.settings.network_access_default
    return True
