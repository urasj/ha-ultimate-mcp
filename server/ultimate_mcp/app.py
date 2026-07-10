"""FastMCP wiring: the ~10 real MCP tools + bearer auth + /health (W0).

Transport: streamable HTTP on 0.0.0.0:8099/mcp, bearer-token gated.
/health is served WITHOUT auth so the Supervisor watchdog can poll it.
"""

from __future__ import annotations

import hmac
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
    """Server health: loaded surfaces, tool counts."""
    total = len(_registry.tools)
    avail = sum(1 for t in _registry.tools.values() if t.available)
    return {"status": "ok", "tools_total": total, "tools_available": avail}


class BearerAuthASGI:
    """ASGI wrapper: constant-time bearer check on everything except GET /health."""

    def __init__(self, inner: Any, token: str) -> None:
        self.inner = inner
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.inner(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("path") == "/health":
            await self._respond_health(send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {self.token}"
        if not (auth and hmac.compare_digest(auth, expected)):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b'{"error":"unauthorized"}'}
                )
            return
        await self.inner(scope, receive, send)

    @staticmethod
    async def _respond_health(send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"status":"ok"}'})


def build_asgi_app() -> Any:
    token = _ctx.options.get("auth_token", "")
    if not token:
        raise SystemExit("auth_token is empty — set it in the add-on options")
    _registry.load_manifests()
    log.info("loaded %d virtual tools", len(_registry.tools))
    try:
        inner = mcp.http_app(stateless_http=True)
    except TypeError:  # older fastmcp without stateless_http kwarg
        inner = mcp.http_app()
    return BearerAuthASGI(inner, token)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("UMCP_LOG_LEVEL", "info").upper())
    uvicorn.run(build_asgi_app(), host="0.0.0.0", port=8099, log_level="info")


if __name__ == "__main__":
    main()
