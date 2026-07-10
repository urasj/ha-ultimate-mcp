# Changelog

## 0.1.0

- Initial scaffold: FastMCP core, search-based tool discovery, safety kernel
  (tiers, checkpoints, journal), fingerprint collector, database/ reference surface.

## 0.1.1

- Fix Claude Desktop / mcp-remote connectivity: serve stateful streamable-HTTP
  (GET /mcp SSE stream + session id) instead of stateless, and return 404 (not
  401) on OAuth-discovery probes so clients fall back to the static bearer token.

## 0.2.0

- Complete tool catalog: 131 tools across 14 surfaces. Adds supervisor, hacs,
  filesystem, dashboards, diagnostics, stats_repair, network (MQTT), zigbee (ZHA),
  realtime (wait/subscribe/watchdog), registries, assist (conversation/pipeline),
  and media_camera surfaces. Fingerprint now emits integration:* capabilities for
  accurate gating. 190 tests.
