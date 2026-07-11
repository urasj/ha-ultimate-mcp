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

## 0.2.1

- umcp_call now coerces a stringified `args` back into an object, so
  parameterized tools work from MCP clients that serialize nested args
  (e.g. Cowork). Unblocks all write tools from those clients.

## 0.2.2

- Implement `main()` so the ASGI server actually starts under s6 / `python -m`.

## 0.2.3

- File re-uploads / packaging fixes; no functional server changes.

## 0.2.4

- **Fix T3 confirm_token never minted (gate was unpassable):** `mint_token`
  existed but no code path called it. T3 dry-runs now return a `confirm_token`
  bound to (tool + canonicalized args hash), single-use, 15-min TTL, with
  distinct rejection reasons (`token_missing`, `token_unknown`, `token_expired`,
  `token_args_mismatch`).
- **Fix T2 checkpoint gate never satisfied:** the real defect was that
  `umcp_call` only forwarded `dry_run=True` into tool args — an apply re-ran
  every impl's dry-run branch (they all default `dry_run=True`), returning the
  plan + `checkpoint_required` forever. The caller's `dry_run` flag is now
  always forwarded. Checkpoints register with `{checkpoint_id, created_at,
  scope}` in a process-wide (not per-session) registry and satisfy the gate
  within a TTL (default 30 min, `checkpoint_ttl_seconds` option);
  `external_checkpoint_ref` is accepted verbatim and recorded in the journal.
- T2+/T3 dry-run responses now state exactly what the apply requires:
  checkpoint status (`satisfied` true/false + remediation) and, for T3, the
  token value itself.
- Every T1+ apply is journaled through the gate layer (with the tool's undo
  copy attached when available), and `umcp_undo` can now also replay
  StorageEditor entries (`undo_id` + `files`), so T1/T2 undo works end-to-end.
- New gate-layer CI smoke tests (full dry-run/apply/journal/undo cycle for one
  tool per tier) + token/checkpoint TTL unit tests; CI now also runs
  `compileall` and an import-all guard against truncated modules.
- `addon_logs` fixed: Supervisor GET now decodes by content-type (the logs
  endpoints return text/plain, not JSON).
- New `go2rtc_url` add-on option plumbed to the media_camera surface
  (`stream_health_report` etc.) for go2rtc running as a separate add-on.
