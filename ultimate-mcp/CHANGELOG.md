# Changelog

## 0.1.0

- Initial scaffold: FastMCP core, search-based tool discovery, safety kernel
  (tiers, checkpoints, journal), fingerprint collector, database/ reference surface.

## 0.1.1

- Fix Claude Desktop / mcp-remote connectivity: serve stateful streamable-HTTP
  (GET /mcp SSE stream + session id) instead of stateless, and return 404 (not
  401) on OAuth-discovery probes so clients fall back to the static bearer token.
