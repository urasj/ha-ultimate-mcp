# ha-ultimate-mcp

The Ultimate MCP Server for Home Assistant — a Supervisor add-on exposing the deep,
system-level toolset no other HA MCP server covers: recorder-DB SQL analytics,
`.storage` registry surgery with a safety spine, real-time wait/subscribe tools,
MQTT broker introspection, ZHA protocol depth, Supervisor/OS/host ops, statistics
repair, profiling, and a full installation fingerprint that auto-gates tools to
what your box actually has.

Designed to coexist with [ha-mcp](https://github.com/homeassistant-ai/ha-mcp)
(which owns the core-API CRUD tier). See `BLUEPRINT.md` for the full tool catalog
and `architecture.md` for design.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
   `https://github.com/urasj/ha-ultimate-mcp`
2. Install **Ultimate MCP**, set an `auth_token` in options, start.
3. Connect your MCP client to `http://<ha-ip>:8099/mcp` with the bearer token.

## Deploy loop

Push to `main` → GitHub Actions builds per-arch images to GHCR → bump `version:`
in `ultimate-mcp/config.yaml` + tag → the add-on store offers the update.

## Safety model

T0 read / T1 reversible / T2 checkpoint-required / T3 destructive (opt-in +
confirm-token handshake). Every mutation is dry-run-first and journaled with undo
artifacts. `.storage` writes go through a stop→backup→atomic-edit→validate→start
editor with automatic rollback.

## Layout

- `ultimate-mcp/` — the add-on (config.yaml, Dockerfile, s6 run script)
- `server/ultimate_mcp/` — the FastMCP server package
- `server/ultimate_mcp/tools/<surface>/` — manifest.py (data) + impl.py (lazy-loaded)
- `.github/workflows/` — CI + per-arch GHCR image builds
