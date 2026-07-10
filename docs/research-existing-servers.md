# Existing Home Assistant MCP Servers — Landscape & Gap Analysis (July 2026)

**Method note.** ha-mcp findings are **verified live** — the user's actual ha-mcp 7.11.0.dev1828 instance is connected to this session and its tool catalog was enumerated via `ha_search_tools`, cross-checked against the master README (dev channel tracks master, so README == what the user runs). Official integration facts verified against the live home-assistant.io page (site shows HA 2026.6.4 docs). Other servers: fetched READMEs where possible; items marked *(inference)* or *(stale?)* where not.

---

## 1. Official `mcp_server` integration (home-assistant/core)

- **Docs:** https://www.home-assistant.io/integrations/mcp_server/ (verified 2026-07)
- **Source:** https://github.com/home-assistant/core/tree/dev/homeassistant/components/mcp_server — maintainer @allenporter, Silver quality, 2.8% of installs
- **Transport/auth:** Streamable HTTP at `/api/mcp`, stateless; OAuth (IndieAuth, client ID = client app base URL) or long-lived access token
- **Deployment:** in-core integration, config flow; introduced 2025.2

**What it exposes:** only the **Assist API**. Tools = the Assist intents (HassTurnOn/Off, climate/media intents, todo, `GetLiveContext`), scoped to entities *exposed to Assist*. One read-only resource: `homeassistant://assist/context-snapshot` (only when `GetLiveContext` is in the LLM API). Prompts supported; **sampling and notifications not supported**.

**Cannot do:** anything structural — no automations/scripts/dashboards CRUD, no registries, no history/statistics, no Supervisor, no files, no logs, no traces. This is by design; the Open Home Foundation roadmap issue asking to extend it to "building, not just controlling" is still open: https://github.com/OpenHomeFoundation/roadmap/issues/97

## 2. ha-mcp (homeassistant-ai/ha-mcp) — what the user runs; THE benchmark

- **Repo/docs:** https://github.com/homeassistant-ai/ha-mcp • https://homeassistant-ai.github.io/ha-mcp/
- **Stack:** Python / FastMCP. **Transport:** stdio (uvx/pip), HTTP/OAuth (Docker), HAOS add-on (with a webhook-proxy add-on to ride Nabu Casa), plus a HACS custom component that runs the whole server in-process.
- **Discovery:** 84-tool catalog, optional BM25 search mode (`ha_search_tools` + `ha_call_read_tool` / `ha_call_write_tool` / `ha_call_delete_tool` proxy split for per-category permission policies). **Skills** bundled as `skill://` MCP resources + `ha_get_skill_guide` fallback. Settings-UI sidecar (`settings_url`), Read Only Mode, per-tool enable/disable, per-tool approval-rule DSL, automatic edit backups, update self-check.

**Tool categories (84 tools, verified):** Add-ons (get/manage incl. **ingress HTTP+WebSocket proxy to any add-on API**, array_patch for Node-RED-style endpoints, sandboxed `python_transform` post-processing); Areas & Floors; Assist pipeline mgmt; Automations CRUD; Blueprints (get/import only); Calendar events CRUD; Camera snapshot; Dashboard screenshot *(beta)*; Dashboards CRUD + resources; Device registry (get/set/remove); Energy prefs; Entity registry (get/set/remove, Assist exposure); Files *(beta — allowlisted paths only)*; Groups; HACS (info/manage); Helpers (28 types); History & Statistics (read), automation traces, logs (logbook / system_log / error_log / add-on / journald system services / logger levels); Integrations (get, system health incl. repairs, ZHA/Z-Wave/Thread/Matter summaries, per-integration diagnostics dumps, config_check, dead-entity detection); Labels & Categories; Scenes; Scripts; Search (fuzzy + deep config search, overview); Service & device control (any service, bulk, fire events); System (YAML edit *(beta, allowlisted top-level keys)*, updates, backup/restore, custom tool *(beta)*, themes, reload, restart); Todo; Utilities (template eval, issue reporting); Zones.

**The sleeper:** `ha_manage_custom_tool` *(beta)* — sandboxed Python "code mode" with `api_get`/`api_post` (REST), `ws_send` (**arbitrary HA WebSocket command**), `call_tool`, and persistable saved tools. This means ha-mcp can *reach* most core WS/REST surfaces ad hoc. What it still can't reach: anything **outside HA core's API** (shell, DB file, .storage writes, broker sockets, host), long-lived subscriptions, and non-HA processes.

**Verified hard limits of ha-mcp (the gap targets):**

| Limit | Detail |
|---|---|
| File allowlist | Read: config YAML, packages, www, themes, custom_templates, dashboards, `custom_components/**/*.py` (read-only), logs, configured /share /media /ssl /backup. **No `.storage/` read or write. No blueprint or custom_component writes.** secrets.yaml masked |
| YAML edit allowlist | Only YAML-only integration keys (command_line, rest, shell_command, notify, knx, lovelace.dashboards, themes). Can't touch `frontend:`, `recorder:`, `http:`, `logger:` etc. |
| No shell | No `ha` CLI, no host exec, no docker exec, no SSH |
| No DB | History/statistics via recorder API only; no SQL, no schema inspection, no statistics import/adjust tools |
| No streaming | Request/response only; no event subscription, no "wait for state", no log follow |
| No MQTT layer | Can configure Mosquitto add-on but cannot publish/subscribe/dump topics (`mqtt.publish` service via ha_call_service is fire-and-forget only) |
| No host/OS ops | No host reboot/shutdown, OS update, datadisk, network config, mounts, resolution checks, hardware info |
| Protocol depth | ZHA = read summary + diagnostics; no cluster attribute R/W, no coordinator/NVM backup, no channel change. Z-Wave = status summary only |
| No profiling | No profiler services orchestration, no debugpy, no py-spy |
| Sandbox ≠ runtime | `python_transform`/custom tool run in the MCP process sandbox — no imports, no sockets, no files *(inference from design; verify sandbox rules in repo)* |

## 3. voska/hass-mcp

- **Repo:** https://github.com/voska/hass-mcp • https://pypi.org/project/hass-mcp/ • Docker `mcpcommunity/voska-hass-mcp`
- **Stack:** Python, stdio via Docker/uv. Auth: long-lived token. ~13 tools: `get_version`, `get_entity`, `entity_action`, `list_entities`, `search_entities_tool`, `domain_summary_tool`, `list_automations`, `call_service_tool`, `restart_ha`, `get_history`, `get_error_log` + guided *prompts* (create automation, debug automation, troubleshoot entity).
- **Cannot do:** any config CRUD (automations are prompt-guided, not written), registries, dashboards, Supervisor, files, HACS. Development appears dormant since 2025 *(stale? — low commit activity, not re-verified in depth)*.

## 4. tevonsb/homeassistant-mcp

- **Repo:** https://github.com/tevonsb/homeassistant-mcp (README fetched)
- **Stack:** TypeScript/Node 20, Docker Compose or npm; token auth + rate limiting; exposes an HTTP API alongside MCP.
- **Tools:** `control` (typed device control per domain), `addon` (Supervisor list/install/start/stop), `package` (HACS integrations/themes/appdaemon/netdaemon), `automation_config` (create/duplicate/enable), history. **Distinctive:** SSE endpoint for **real-time state/automation/service events** (`/subscribe_events`) — the only surveyed server with live push, though it's a side-channel HTTP API, not MCP-native.
- **Cannot do:** dashboards, helpers, registries, files, logs/traces, statistics. Roadmap section shows WebSocket support still "in progress" — project effectively stalled *(inference from README status list)*.

## 5. Others (brief)

| Server | Notes |
|---|---|
| jango-blockchained/homeassistant-mcp | Bun/TypeScript fork-lineage of tevonsb with NLP/speech extras (wake word, STT). Same structural limits. *(not re-fetched; training knowledge)* |
| zorak1103/ha-mcp | https://github.com/zorak1103/ha-mcp — smaller Python server: states/services/automation mgmt. Subset of ha-mcp |
| achetronic/hass-mcp | https://github.com/achetronic/hass-mcp — Go, remote-first (OAuth/SSE), control+query focused |
| astromechza/ha-mcp-server | https://github.com/astromechza/ha-mcp-server — deliberately minimal wrapper over HA API |
| hass-mcp-plus | https://pypi.org/project/hass-mcp-plus/ — community fork of voska's, adds dashboards/config *(unverified)* |
| SmartHomeScene guide | https://smarthomescene.com/guides/home-assistant-mcp-server-complete-guide/ — landscape overview, confirms official-vs-community split |

---

## 6. Gap table — capabilities NO surveyed server covers

Scored against the strongest competitor (always ha-mcp unless noted). "Partial" = reachable awkwardly via ha-mcp's code-mode/proxy but not a real tool.

| # | Capability | Best existing | Gap for our server |
|---|-----------|---------------|--------------------|
| G1 | **Direct recorder DB SQL** (SQLite/MariaDB): analytics, DB-bloat audit, purge preview, per-entity storage cost | none | Full — we run on-box, can open `home-assistant_v2.db` read-only |
| G2 | **`.storage` registry surgery** (read + guarded write: orphan cleanup, bulk entity_id rename w/ reference rewrite, restore_state edits) | none (ha-mcp blocks `.storage` entirely) | Full — highest-value power-user ask |
| G3 | **Real-time subscribe/wait tools** (`wait_for_state`, `subscribe_events` window capture, log follow, trace-on-next-run) | tevonsb SSE (side-channel, stalled) | MCP-native long-poll/notification design |
| G4 | **Shell & `ha` CLI execution** (host journal, `ha core check`, docker ps/stats/inspect via `docker_api`, py-spy) | none | Full — needs `full_access`/`docker_api` add-on flags |
| G5 | **MQTT broker layer**: pub/sub with response capture, retained-topic dump, `$SYS/#` broker stats, `homeassistant/#` discovery audit, Mosquitto creds via Supervisor `/services/mqtt` | none | Full — user runs Mosquitto |
| G6 | **Host/OS/hardware ops**: host reboot/shutdown, OS update/boot-slot, datadisk, network config + Wi-Fi scan, mounts, `/hardware/info`, resolution checks/suggestions | none (ha-mcp stops at add-ons/backups) | Full — Supervisor API already there |
| G7 | **Zigbee deep tools**: `zigbee.db` neighbor-table topology graph, cluster attribute read/write, coordinator/NVM backup, channel-energy scan | ha-mcp read-only summary | Deep half missing |
| G8 | **Z-Wave JS driver API** (direct ws to zwave-js-server: NVM backup, RSSI, rebuild routes, firmware OTA) | ha-mcp status summary | Missing (N/A on this box today — no Z-Wave in fingerprint; low priority) |
| G9 | **Core profiling/debugging**: profiler.* orchestration (cProfile, objgraph, lru_stats), debugpy enable, memory-leak workflow | none | Full |
| G10 | **Blueprint authoring** (write/save/substitute, not just import) + **custom_component scaffold/deploy/patch + restart loop** | ha-mcp import-only, cc read-only | Full — "AI writes an integration" story |
| G11 | **Typed Frigate/go2rtc/camera-AI tools** (events/clips queries, frame grab at go2rtc, LLM Vision chaining) | ha-mcp generic ingress proxy (untyped) | Typed tools + MCP image content |
| G12 | **Statistics repair**: `recorder/import_statistics`, `adjust_sum_statistics`, fix broken energy data | none | Full |
| G13 | **Assist/conversation test harness**: run `conversation/process` & `assist_pipeline/run` against agents, diff responses, exposure lint | ha-mcp manages pipelines but can't run/test them | Full |
| G14 | **Dependency/impact graph**: what breaks if entity X renamed — automations, scripts, dashboards, groups, template refs (cross YAML+storage+DB) | ha-mcp deep search (text-level) | Structured graph missing |
| G15 | **Scheduled server-side jobs/watchdogs** (cron'd health report, entity flatline detector) | none | Full — we're resident on the box |
| G16 | **Backup content introspection** (list files inside a backup tar, selective extract/diff) | ha-mcp create/restore only | Full |

**Bottom line:** ha-mcp owns the "HA-core-API CRUD" tier so thoroughly (plus an escape-hatch code mode) that duplicating it is pointless. Everything defensible for us lives **below** the core API (DB, .storage, shell, broker, radios, process) or **across time** (subscriptions, schedules, watchdogs) — exactly the surfaces only reachable because we run on the box as a privileged add-on.

### Sources
- https://www.home-assistant.io/integrations/mcp_server/ • https://github.com/OpenHomeFoundation/roadmap/issues/97
- https://github.com/homeassistant-ai/ha-mcp (README master, fetched) • https://homeassistant-ai.github.io/ha-mcp/ • https://github.com/homeassistant-ai/ha-mcp/blob/master/homeassistant-addon/DOCS.md
- Live tool-catalog enumeration of the user's ha-mcp 7.11.0.dev1828 via `ha_search_tools` (this session)
- https://github.com/voska/hass-mcp • https://pypi.org/project/hass-mcp/ • https://hub.docker.com/r/mcpcommunity/voska-hass-mcp
- https://github.com/tevonsb/homeassistant-mcp (README fetched)
- https://github.com/zorak1103/ha-mcp • https://github.com/achetronic/hass-mcp • https://github.com/astromechza/ha-mcp-server
- https://smarthomescene.com/guides/home-assistant-mcp-server-complete-guide/
