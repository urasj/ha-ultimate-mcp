"""FastMCP wiring: the ~10 real MCP tools + auth + /health (W0).

Transport: streamable HTTP on 0.0.0.0:8099/mcp, bearer-token gated.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import FastMCP

from ultimate_mcp.context import Context
from ultimate_mcp.fingerprint.collect import collect_fingerprint
from ultimate_mcp.registry import Registry
from ultimate_mcp.safety.kernel import SafetyKernel

log = logging.getLogger("umcp")

mcp: FastMCP = FastMCP("ultimate-mcp")
_ctx = Context()
_registry = Registry()
_safety = SafetyKernel(_ctx)


@mcp.tool()
async def umcp_fingerprint(refresh: bool = False) -> dict[str, Any]:
    """Machine-readable profile of this HA installation (drives capability gating)."""
    if refresh or not _ctx.fingerprint:
        _ctx.fingerprint = await collect_fingerprint(_ctx)
        _registry.apply_gates(_ctx.fingerprint)
    return _ctx.fingerprint


@mcp.tool()
async def umcp_search_tools(
    query: str, surface: str | None = None, max_results: int = 10
) -> list[dict[str, Any]]:
    """Search the virtual tool catalog. Returns schemas + tier annotations for matches."""
    return _registry.search(query, surface=surface, max_results=max_results)


@mcp.tool()
async def umcp_describe_tool(name: str) -> dict[str, Any]:
    """Full description + JSON schema for one virtual tool."""
    return _registry.describe(name)


@mcp.tool()
async def umcp_call(
    name: str,
    args: dict[str, Any] | None = None,
    dry_run: bool = True,
    confirm_token: str | None = None,
    external_checkpoint_ref: str | None = None,
) -> Any:
    """Invoke a virtual tool. Mutating tools default to dry_run=True and return a
    plan + confirm_token; call again with dry_run=False (+ token for T3)."""
    args = args or {}
    await _safety.authorize(_registry, name, dry_run, confirm_token, external_checkpoint_ref)
    if dry_run:
        args["dry_run"] = True
    return await _registry.dispatch(_ctx, name, args)


@mcp.tool()
async def umcp_checkpoint(scope: str = "homeassistant", name_hint: str = "manual") -> dict:
    """Create a Supervisor partial backup as a pre-change checkpoint."""
    return await _safety.checkpoint(scope, name_hint)


@mcp.tool()
async def umcp_journal(limit: int = 20) -> list[dict[str, Any]]:
    """Recent change-journal entries (every mutation is recorded)."""
    return _safety.journal_tail(limit)


@mcp.tool()
async def umcp_undo(entry_id: str) -> dict[str, Any]:
    """Undo a journaled change by replaying its inverse operation."""
    return await _safety.undo(entry_id)


@mcp.tool()
async def umcp_health() -> dict[str, Any]:
    """Server health: loaded surfaces, tool counts, WS connection state."""
    total = len(_registry.tools)
    avail = sum(1 for t in _registry.tools.values() if t.available)
    return {"status": "ok", "tools_total": total, "tools_available": avail}


def main() -> None:
    logging.basicConfig(level=os.environ.get("UMCP_LOG_LEVEL", "info").upper())
    token = _ctx.options.get("auth_token", "")
    if not token:
        raise SystemExit("auth_token is empty — set it in the add-on options")
    _registry.load_manifests()
    log.info("loaded %d virtual tools", len(_registry.tools))
    # NOTE(W0): add bearer-token middleware + /health route on the underlying app,
    # verify stateless_http for multi-client LAN use.
    mcp.run(transport="http", host="0.0.0.0", port=8099)


if __name__ == "__main__":
    main()
