# Ultimate MCP Server for Home Assistant — Master Blueprint

Companion files: `architecture.md` (packaging, core, safety, workstreams), `research-existing-servers.md` (landscape + gap analysis), `research-api-surface.md` (every HA control surface, verified sources).

## Grounding (your actual box, fingerprinted live)

HAOS 2026.7.1 on Proxmox VM 100 (`communewayhomeassistant`, proxmox1, 2 vCPU, 10 GB RAM at 95%). 1,152 entities / 39 domains / 497 services / 23 areas. Installed: HACS, Mosquitto, go2rtc, Advanced SSH & Web Terminal, browser_mod, LLM Vision, HA Cloud, Google AI agents, Alexa/Fire TV/Sonos/LG heavy usage, ZHA-style Zigbee button events, SQLite recorder. Already running ha-mcp 7.11.0.dev (84 tools, core-API CRUD tier).

**Strategy:** don't duplicate ha-mcp. Own everything **below the core API** (DB, .storage, shell, MQTT broker, radios, process internals) and **across time** (subscriptions, watchdogs, schedules). These are reachable only because umcp runs on-box as a privileged add-on.

## The full tool catalog (~150 virtual tools, 12 surfaces)

Tiers: T0 read / T1 reversible write / T2 risky write (checkpoint required) / T3 destructive (opt-in + confirm token). `gate:` = capability predicate from the fingerprint.

### 1. fingerprint/ (T0)
- `fingerprint_full` — complete install profile (core, supervisor, OS, host, hardware, add-ons, integrations, custom components, HACS inventory, DB engine+size, recorder settings, network)
- `fingerprint_refresh`, `fingerprint_diff` — re-profile and diff vs last (what changed since yesterday?)
- `breaking_change_scan` — installed integrations vs release-notes breaking changes for target version
- `upgrade_readiness` — pre-update audit: deprecated YAML, dead custom components, pending repairs, DB size, backup freshness
- `hardware_audit` — USB/serial passthrough visibility from the VM (Zigbee stick health)

### 2. database/ (SQLite ro by default; gate: db:sqlite|mariadb)
- T0: `db_query` (read-only SQL, `mode=ro` + `PRAGMA query_only`), `db_schema`, `db_size_report`, `db_entity_cost` (rows+bytes per entity), `db_churn_top` (noisiest entities), `db_event_firehose` (noisy integrations by event volume), `db_stats_gaps` (statistics anomalies/gaps), `db_purge_preview` (what a purge would delete), `db_recorder_advisor` (recommended recorder excludes with projected savings), `db_restart_history` (recorder_runs), `db_integrity_check`
- T2: `db_purge_execute` (via recorder.purge/purge_entities services, never raw DELETE)
- T3: `db_execute` (raw write SQL, WAL-safe, checkpoint + confirm token)

### 3. storage/ (.storage registry surgery — the killer feature; all writes via stop→backup→atomic-edit→validate→start protocol)
- T0: `storage_read` (any .storage file, secrets masked), `storage_orphan_scan` (registry entries whose device/config-entry is gone), `storage_restore_state_view`, `dependency_graph` (what references entity X across automations, scripts, dashboards, groups, templates, YAML + .storage + DB)
- T2: `entity_rename_deep` (bulk entity_id rename WITH reference rewrite everywhere the graph found), `storage_orphan_clean`, `restore_state_edit`, `storage_patch` (guarded JSON patch on any .storage file), `config_entry_surgery` (fix broken config entries the UI can't)

### 4. filesystem/ (map: homeassistant_config rw)
- T0: `fs_read`, `fs_grep`, `fs_tree`, `yaml_lint`, `secrets_audit` (unused/missing secrets), `custom_component_inventory`
- T1: `fs_write_www` (deploy to /local), `theme_write`, `blueprint_write` (authoring, not just import)
- T2: `yaml_edit_any` (full configuration.yaml incl. recorder:/http:/logger: blocks ha-mcp can't touch; always chained with `ha core check`), `custom_component_scaffold`, `custom_component_patch` + reload/restart loop ("AI writes an integration"), `addon_file_deploy` (into other add-ons' config dirs via all_addon_configs — the init_commands successor)
- T0: `backup_tar_list`, `backup_tar_extract`, `backup_tar_diff` (crack open backup tars; diff config vs 30 days ago)

### 5. realtime/ (the cross-time tier; MCP long-poll design)
- `wait_for_state(entity, to, timeout)` — block until state change
- `event_window_capture(filter, seconds)` — record bus events for N seconds, return the batch
- `log_follow(source, seconds|until_match)` — tail core/add-on/journald logs with pattern stop
- `trace_next_run(automation)` — arm, wait for the next trigger, return the full trace
- `state_flatline_scan` — entities that stopped updating (dead sensors)
- `watchdog_create/list/delete` — resident watchdogs (entity flatline, add-on crash-loop, disk threshold) that fire persistent notifications
- `scheduled_report_create` — cron'd on-box health report to a notify target

### 6. supervisor/ (hassio_role: manager)
- T0: `addon_list/info/stats/logs` (incl. boot-scoped logs), `core_stats`, `os_info`, `host_info`, `host_disk_usage`, `resolution_center_report` (checks + suggestions), `jobs_list`, `update_inventory` (everything updatable in one view)
- T1: `resolution_apply_suggestion`, `addon_options_set` (+auto-restart chain), `addon_stdin`
- T2: `addon_install/update/restart/rebuild`, `core_restart` (always preceded by `core_check`), `store_repo_add`, `backup_freeze/thaw` (consistent DB backups), `backup_partial/full`, `dns_override`
- T3: `addon_uninstall`, `os_update`, `host_reboot` (requires admin role upgrade — documented path, off by default)

### 7. network/ (gate: addon:core_mosquitto for mqtt_*)
- T0: `mqtt_broker_stats` ($SYS/#), `mqtt_discovery_audit` (orphaned/duplicate discovery configs), `mqtt_retained_dump`, `mqtt_subscribe_window` (capture topic traffic for N seconds), `net_info`, `wifi_scan` (Supervisor accesspoints)
- T1: `mqtt_publish_with_response` (pub + wait for reply topic), `mqtt_retained_clear`
- T0: `speedtest_history` (via recorder), `dns_health`

### 8. zigbee/ (gate: integration:zha)
- T0: `zha_topology_graph` (zigbee.db neighbor/route tables → link-quality map), `zha_device_detail`, `zha_cluster_read` (attribute read), `zha_binding_list`, `zha_network_settings`
- T1: `zha_cluster_write` (manufacturer-specific attributes), `zha_bind/unbind`, `zha_reconfigure`
- T2: `zha_coordinator_backup` (NVM/network backup for coordinator migration/DR)
- (zwave/ surface ships gated OFF — no Z-Wave on this box; auto-enables if fingerprint ever finds it)

### 9. diagnostics/ (T0 unless noted)
- `system_log_triage` (structured error/warning clustering), `logger_set_level` (T1, per-integration debug toggles), `integration_diagnostics_dump`, `profiler_cpu` (orchestrate profiler.start → fetch .prof artifact), `profiler_memory` (objgraph/dump_log_objects → leak workflow), `lru_stats`, `thread_frames`, `debugpy_enable` (T2), `startup_time_report` (per-integration setup times), `repair_list/apply` (T1)

### 10. stats_repair/ (energy-data fixes nothing else offers)
- T0: `stats_list/metadata`, `stats_anomaly_scan` (spikes/negatives/unit mismatches in long-term stats)
- T2: `stats_import` (recorder/import_statistics), `stats_adjust_sum` (fix broken energy totals), `stats_change_unit`, `stats_clear` (T3)

### 11. assist/ (gate: integration:conversation; your Google AI agents)
- T0: `conversation_test` (run conversation/process against an agent, return response + intent trace), `pipeline_run` (assist_pipeline/run harness), `pipeline_debug_trace`, `agent_diff` (same utterance across two agents, diff), `assist_exposure_lint` (entities exposed to Assist that shouldn't be, and vice versa)

### 12. media_camera/ (gate: addon go2rtc / integrations present)
- T0: `go2rtc_streams`, `go2rtc_frame_grab` (returns MCP image content), `camera_snapshot_all`, `stream_health_report`
- T1: `llm_vision_analyze` (frame → LLM Vision chain as one tool)
- T0: `media_index` (/media, /share inventory)

Plus surface 0, always flat: `umcp_search_tools`, `umcp_describe_tool`, `umcp_call`, `umcp_checkpoint`, `umcp_undo`, `umcp_journal`, `umcp_health`, `umcp_fingerprint`, `umcp_enable_surface`.

## Safety spine (applies to everything)

Dry-run default-on for all mutating tools → structured diff + confirm token. T2+ requires a same-session checkpoint (Supervisor partial backup, verified on /backup). T3 additionally requires `destructive_enabled: true` in add-on options + two-call confirm handshake + optional Proxmox snapshot hint (`{node: proxmox1, vmid: 100}`) that the client executes via its Proxmox MCP and passes back as `external_checkpoint_ref`. All changes journaled (JSONL + undo artifacts); `umcp_undo` replays inverses. `ha core check` gates every restart. `.storage` writes only through the stop→copy→edit→validate→start editor with 120s auto-rollback.

## Developer agent team

| Agent | Workstream | Scope |
|---|---|---|
| Core Lead | W0 | app, registry, context facades, safety kernel, fingerprint — freezes the contract everyone codes against |
| Platform Dev | W1 | supervisor/ + hacs/ surfaces |
| Config Surgeon | W2 | filesystem/ + storage/ + dashboards — the .storage editor is theirs |
| Data Engineer | W3 | database/ + diagnostics/ + stats_repair/ |
| Protocol Dev | W4 | network/ (MQTT) + zigbee/ + realtime/ |
| Integrations Dev | W5 | registries/ + assist/ + media_camera/ |
| Release Engineer | W6 | add-on packaging, Dockerfile, GHCR builds, CI, on-box smoke test |

Rules: W0 merges first; W1–W5 run in parallel, one PR per surface (manifest + impl + recorded-fixture tests); every tool must be schema-validated, tiered, gated, dry-runnable, and searchable before merge. W6 cuts `0.1.0` when the smoke test (install from GitHub repo, `/health`, one T0 call per enabled surface) passes on the real box.

## Build phases

1. **Phase 0 (contract):** W0 core + packaging skeleton → installable add-on that serves `umcp_fingerprint` and `umcp_search_tools` with an empty catalog. Proves the GitHub→add-on-store loop end to end.
2. **Phase 1 (highest-value gaps):** database/, storage/ (with safety spine), realtime/ basics (`wait_for_state`, `log_follow`). This alone exceeds every existing server.
3. **Phase 2:** supervisor/, network/ (MQTT), diagnostics/.
4. **Phase 3:** zigbee/, stats_repair/, assist/, media_camera/, watchdogs.
5. **Phase 4:** polish — dependency_graph performance, breaking-change scanner, flat-mode promotion for hot surfaces.

## Verify-before-build list (flagged by both agents)

Exact 2026.7 WS command spellings (backup/*, recorder/*, zha/*, hacs/*); recorder DB schema revision (read `schema_changes` at runtime, never hardcode); Supervisor `map:` long-form type names and role capability matrix; HA base-image tag; FastMCP `stateless_http` behavior for multi-client LAN use. Resolution: Phase 0 includes a `probe` script that tests each assumption against the live box and writes results into the fingerprint fixture.
