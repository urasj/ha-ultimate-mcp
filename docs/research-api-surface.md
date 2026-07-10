# Home Assistant Control/Inspection Surface — Exhaustive Enumeration

**Vantage point:** a Python process running **as a Home Assistant OS add-on** on the target Proxmox VM (HAOS, core 2026.7.1, Supervisor-managed). Risk levels are for a tool built on the surface: **Low** = read-only/safe, **Med** = writes recoverable state, **High** = can break HA/host or lose data.

**Access-method legend (add-on manifest flags):**
- `homeassistant_api: true` → env `SUPERVISOR_TOKEN` proxies to Core REST at `http://supervisor/core/api/...`
- `hassio_api: true` + `hassio_role: manager|admin` → Supervisor API at `http://supervisor/...`
- `auth_api: true` → validate HA users
- `full_access: true` → privileged container (host devices, no AppArmor confinement) — **cannot combine with most other API flags per Supervisor rules; verify at build**
- `docker_api: true` → read Docker/container info
- `map:` → mount host shares: `config:rw`, `share`, `ssl`, `addons`, `backup`, `media`, `all_addon_configs`
- `devices:`/`uart`/`usb` → hardware passthrough; `host_network`, `host_pid`, `host_dbus`
Source: https://developers.home-assistant.io/docs/add-ons/configuration/ and Supervisor endpoints https://developers.home-assistant.io/docs/api/supervisor/endpoints/ (both verified 2026-07).

---

## 1. Core REST API (`/api/...`)
Via `homeassistant_api` + `SUPERVISOR_TOKEN`. Docs: https://developers.home-assistant.io/docs/api/rest/

| Endpoint | Enables | Risk |
|---|---|---|
| `GET /api/states`, `/states/<eid>`, `POST /states/<eid>` | read all states; **push virtual/override states** (not backed by an integration) | Low/Med |
| `POST /api/services/<d>/<s>`, `GET /api/services` | call any service; service catalog | Med |
| `GET /api/config`, `/api/config/core/check_config` | core config, pre-restart validation | Low |
| `GET /api/error_log`, `/api/logbook`, `/api/history/period` | raw log, logbook, history | Low |
| `POST /api/template` | render Jinja server-side | Low |
| `GET /api/calendars`, `/api/camera_proxy/<eid>` | calendar list, **camera frame bytes** (image content) | Low |
| `POST /api/events/<type>`, `GET /api/events` | fire/list bus events | Med |
| `GET /api/config/<flow>` (REST config-entry endpoints) | some config-entry CRUD | Med |

**Note:** REST is a thin subset. The registries, statistics, and most 2026-era features are **WebSocket-only** — see §2. ha-mcp already wraps almost all of REST; little unique tool value here.

## 2. Core WebSocket API (`/api/websocket`, or add-on `/core/websocket`)
The deep surface. Auth with `SUPERVISOR_TOKEN`. Command list (not fully centralized in docs — cross-referenced against core source & the community gist https://gist.github.com/mhagger/f1cc7844a7736bd5258d953e0a22b398). Docs: https://developers.home-assistant.io/docs/api/websocket/

| Command family | Key commands | Unique tools it enables | Risk |
|---|---|---|---|
| **State/events (streaming)** | `subscribe_events`, `subscribe_trigger`, `subscribe_entities`, `unsubscribe_events`, `fire_event` | **`wait_for_state` / event-window capture / live watchdog** — the whole real-time tier ha-mcp lacks | Low–Med |
| **Entity registry** | `config/entity_registry/list`, `list_for_display`, `get`, `update`, `remove` | bulk rename, disable, area/label/category assignment, hidden/entity-category edits | Med |
| **Device registry** | `config/device_registry/list`, `update`, `remove` | device area/name-by-user, disable | Med |
| **Area/Floor/Label/Category registry** | `config/area_registry/*`, `config/floor_registry/*`, `config/label_registry/*`, `config/category_registry/*` | full org-graph CRUD | Med |
| **Config entries** | `config_entries/get`, `config_entries/subentries/*`, `config_entries/flow/*`, `config_entries/options/*`, `.../disable`, `.../reload` | integration setup/options flows, reload, enable/disable, subentries | Med–High |
| **Statistics** | `recorder/statistics_during_period`, `recorder/list_statistic_ids`, `recorder/get_statistics_metadata`, `recorder/update_statistics_metadata`, `recorder/import_statistics`, `recorder/adjust_sum_statistics`, `recorder/clear_statistics`, `recorder/change_statistics_unit`, `recorder/info` | **statistics repair/import/adjust** (energy-data fixes) — big gap | Med–High |
| **System log** | `system_log/list`, `system_log/clear` | structured error/warning triage | Low |
| **Logger** | `logger/log_info`, `logger/integration_log_level`, `logger/set_level` | per-integration debug toggles | Low |
| **Trace** | `trace/list`, `trace/get`, `trace/contexts` | automation/script step traces (ha-mcp has traces; blueprints below) | Low |
| **Blueprint** | `blueprint/list`, `blueprint/import`, `blueprint/save`, `blueprint/delete`, `blueprint/substitute` | **blueprint authoring** (ha-mcp is import-only) | Med |
| **Lovelace** | `lovelace/config`, `lovelace/config/save`, `lovelace/resources`, `lovelace/resources/create|update|delete`, `lovelace/dashboards/*` | dashboards (ha-mcp covers) | Med |
| **Automation/Script config** | `config/automation/config/*`, `config/script/config/*`, `config/scene/config/*` | storage-mode CRUD (ha-mcp covers) | Med |
| **Energy** | `energy/get_prefs`, `energy/save_prefs`, `energy/info`, `energy/validate`, `energy/fossil_energy_consumption`, `energy/solar_forecast` | energy dashboard config + validation | Med |
| **Backup (2026)** | `backup/info`, `backup/details`, `backup/generate`, `backup/restore`, `backup/delete`, `backup/config/info`, `backup/config/update`, `backup/subscribe_events`, `backup/agents/info`, `backup/upload`, `backup/can_decrypt_on_download` | native-backup engine incl. **cloud/agent backups & backup config** (superset of Supervisor snapshots) | Med–High |
| **Repairs** | `repairs/list_issues`, `repairs/ignore_issue`, `repairs/get_flow`, `repairs/apply` (flow) | repair inventory + apply fix-flows | Med |
| **Analytics** | `analytics`, `analytics/preferences` | opt-in analytics state | Low |
| **System health** | `system_health/info` | integration health dumps | Low |
| **Assist pipeline** | `assist_pipeline/pipeline/*`, `assist_pipeline/run`, `assist_pipeline/pipeline_debug/*`, `assist_pipeline/device/capture` | **run pipelines / STT-TTS test harness** (ha-mcp only manages, can't run) | Med |
| **Conversation** | `conversation/agent/info`, `conversation/process`, `conversation/prepare` | **test conversation agents** (Google AI agents on this box) | Med |
| **Cloud (Nabu Casa)** | `cloud/status`, `cloud/subscription`, `cloud/remote/connect|disconnect`, `cloud/google_assistant/*`, `cloud/alexa/*`, `cloud/login`, various | Nabu Casa remote/Alexa/Google config (user has HA Cloud) | Med–High |
| **Tags / Persons / Counters** | `tag/*`, `person/*`, `counter/*` etc. | helper CRUD (ha-mcp covers via helpers) | Med |
| **ZHA** | `zha/devices`, `zha/device`, `zha/groups`, `zha/group/*`, `zha/devices/reconfigure`, `zha/devices/clusters`, `zha/devices/clusters/attributes`, `zha/devices/clusters/attributes/value` (read/write), `zha/devices/bindings/*`, `zha/network/settings`, `zha/network/backup`, `zha/topology/update` | **cluster attribute R/W, binding, coordinator/NVM backup, topology** — deep gap | Med–High |
| **Z-Wave JS** | `zwave_js/network_status`, `zwave_js/node_status`, `zwave_js/*` (heal/rebuild routes, NVM backup, firmware update, RSSI) | Z-Wave deep ops (**N/A — no Z-Wave on this box; low priority**) | Med–High |
| **Bluetooth** | `bluetooth/subscribe_advertisements`, `bluetooth/subscribe_connection_allocations` | BLE adapter/advertisement introspection | Low |
| **Template/validation** | `render_template`, `validate_config` | live template + config validation | Low |

**Version-sensitive:** the native **`backup/*`** stack replaced Supervisor snapshots as the primary path in 2025; `restore_state`/statistics command names have shifted across releases — pin to 2026.7 and feature-detect. Command set is **not exhaustively documented**; verify each against core source for 2026.7.

## 3. Supervisor API (`http://supervisor/...`)
Via `hassio_api` + `hassio_role: manager` (admin for host ops). Endpoint list **verified** against https://developers.home-assistant.io/docs/api/supervisor/endpoints/ (fetched — full path inventory below).

| Group | Endpoints (representative) | Unique tools | Risk |
|---|---|---|---|
| **Add-ons** | `/addons`, `/addons/<slug>/info|options|start|stop|restart|rebuild|update|uninstall|stats|logs|logs/follow|logs/latest|logs/boots/<id>`, `/addons/<slug>/stdin`, `/security`, `/sys_options`, `/options/validate`, `/options/config` | lifecycle, **stdin to add-on**, boot-scoped logs, config validate. ha-mcp covers most incl. ingress proxy | Med |
| **Store** | `/store`, `/store/addons`, `/store/addons/<a>/install|update|changelog|documentation|availability`, `/store/reload`, `/store/repositories` (GET/POST/DELETE) | repo add/remove, install (ha-mcp covers) | Med |
| **Core** | `/core/info|check|options|restart|stop|start|rebuild|update|stats|logs|websocket`, `/core/api/...` (REST proxy) | core lifecycle + **`/core/check`** + stats | Med–High |
| **Host** | `/host/info|options|reboot|shutdown|reload`, `/host/services`, `/host/service/<s>/start|stop|reload`, `/host/logs` (+ journald `identifiers/<id>`, `boots/<id>`, follow), `/host/disks/<disk>/usage` | **host reboot/shutdown, systemd service control, journald log access, disk usage** — none in ha-mcp | High |
| **OS** | `/os/info|update|config/sync`, `/os/boot-slot`, `/os/config/swap`, `/os/datadisk/list|move|wipe`, `/os/boards/<board>` (yellow/green/raspberrypi firmware) | HAOS update, boot-slot switch, **datadisk move/wipe**, swap, board firmware | High |
| **Network** | `/network/info`, `/network/interface/<i>/info|update`, `/network/interface/<i>/accesspoints` (**Wi-Fi scan**), `/network/reload`, `/network/interface/<i>/vlan/<id>` | network config, Wi-Fi AP scan, VLAN | High |
| **Hardware** | `/hardware/info` (full device tree, incl. **USB passthrough visibility from the VM**), `/hardware/audio` | serial/USB/GPIO inventory — key for Zigbee stick, VM passthrough audit | Low |
| **Backups** | `/backups`, `/backups/info|options|reload|freeze|thaw`, `/backups/new/full|partial|upload`, `/backups/<b>/download|info|restore/full|restore/partial`, DELETE `/backups/<b>` | **freeze/thaw (consistent DB backup), upload, partial restore, download tar** — ha-mcp has basic backup only | Med–High |
| **Resolution center** | `/resolution/info`, `/resolution/check/<c>/run|options`, `/resolution/suggestion/<s>` (apply/dismiss), `/resolution/issue/<i>`, `/resolution/healthcheck` | **run system checks, apply suggestions** — none in ha-mcp | Med |
| **Jobs** | `/jobs/info|options|reset`, `/jobs/<id>` (GET/DELETE) | track/cancel long Supervisor jobs (backups, updates) | Low |
| **Observer / Multicast / DNS / Audio / CLI** | `/observer/info|stats|update`, `/dns/info|options|reset|logs|stats`, `/multicast/*`, `/audio/*` (PulseAudio: volume/mute/profile/default in-out), `/cli/*` | plugin health/stats, **DNS override**, PulseAudio control | Med |
| **Docker** | `/docker/info`, `/docker/registries` (GET/POST/DELETE), `/docker/options`, `/docker/migrate-storage-driver` | registry creds, docker daemon info | Med |
| **Mounts** | `/mounts` (GET/POST), `/mounts/<name>` (PUT/DELETE), `/mounts/<name>/reload` | network share (CIFS/NFS) mount mgmt | High |
| **Services (discovery)** | `/services`, `/services/mqtt` (GET/POST/DELETE), `/services/mysql` (GET/POST/DELETE) | **read Mosquitto & MariaDB creds/host/port** the Supervisor holds → feeds §7 MQTT & §6 DB tools | Med |
| **Discovery / Ingress / Auth** | `/discovery`, `/discovery/<uuid>`, `/ingress/panels|session|validate_session`, `/auth`, `/auth/list|cache|reset` | integration auto-discovery, ingress sessions, HA-user auth | Med |
| **Updates** | `/available_updates`, `/refresh_updates`, `/reload_updates`, `/supervisor/*` (info/options/update/reload/repair) | update inventory across all components | Med |

## 4. `ha` CLI (via SSH & Web Terminal add-on, or our own shell)
The `ha` binary wraps the Supervisor API but is convenient for shelling. Needs shell access (our add-on or the installed **Advanced SSH & Web Terminal**). Commands: `ha core check|restart|logs`, `ha supervisor logs`, `ha host reboot`, `ha addons`, `ha backups new`, `ha network info`, `ha resolution`, `ha os update`, `ha jobs`. **Unique tool value:** a `run_ha_cli` tool for anything not yet wrapped, plus arbitrary shell (`docker ps/stats/inspect`, `top`, `py-spy`). Risk: **High** (arbitrary command execution).

## 5. Direct filesystem on HAOS
Via `map:` mounts. `/config` (rw), `/share`, `/ssl`, `/media`, `/backup`, `/addons`, and add-on config dirs.

| Path | Tools | Risk |
|---|---|---|
| `/config/*.yaml`, `packages/`, `custom_templates/`, `www/`, `themes/`, `blueprints/` | full YAML read/write **beyond ha-mcp's allowlist** (recorder:, http:, logger:, frontend: blocks) | Med–High |
| `/config/.storage/*` (JSON: `core.entity_registry`, `core.device_registry`, `core.area_registry`, `core.restore_state`, `lovelace.*`, `auth`, `http`, energy, `hacs.*`, integration data) | **the .storage tier** — orphan cleanup, bulk rename+reference-rewrite, restore_state edits, config-entry surgery. ha-mcp blocks this entirely. **Must guard: HA reads .storage only at boot; write→restart, and back up first** | **High** |
| `/config/custom_components/**` | scaffold/patch a custom integration, then reload/restart — "AI writes an integration" | High |
| `/config/deps`, `/config/.cloud` | pip deps cache, Nabu Casa tokens (read for diagnostics; sensitive) | Low/High |
| `/addons/**`, `/backup/*.tar` | inspect local add-on source; **crack open backup tars** to list/extract/diff files | Low–Med |

**Rule:** never write `.storage` while HA is running without a defined restart+backup workflow; prefer WS registry commands (§2) for live edits and reserve raw `.storage` writes for what the API can't express.

## 6. Recorder database (direct SQL)
On this box: default **SQLite** at `/config/home-assistant_v2.db` unless MariaDB add-on is used (Supervisor `/services/mysql` reveals which). Open **read-only** (`file:...?mode=ro`, or a WAL-safe copy) to avoid corrupting the live writer.

| Table | Tools | Risk |
|---|---|---|
| `states`, `states_meta`, `state_attributes` | per-entity row counts & storage cost, high-churn-entity finder, DB-bloat audit, purge preview | Low (ro) |
| `statistics`, `statistics_short_term`, `statistics_meta` | raw long-term stats, gap/anomaly detection, cross-check vs WS statistics | Low (ro) |
| `events`, `event_data`, `event_types` | event-firehose analysis, noisy-integration detection | Low (ro) |
| `recorder_runs`, `schema_changes`, `migration_changes` | restart history, schema version, migration state | Low |

**Unique value:** analytics/optimization tools no API offers (which entities cost the most DB, what to exclude from recorder, why the DB is 8 GB). **Writes = High risk** (corruption) — keep SQL tools read-only; do purges through the `recorder.purge`/`purge_entities` services instead. Version-sensitive: recorder schema rev changes across releases — read `schema_changes` first, don't hardcode columns.

## 7. MQTT broker (Mosquitto) introspection
Creds/host from Supervisor `/services/mqtt` (§3) or add-on options. Connect a paho-mqtt client from the add-on.

| Surface | Tools | Risk |
|---|---|---|
| `homeassistant/#` discovery topics | **audit MQTT discovery** — orphaned/duplicate configs, entities that vanished, retained-config cleanup | Low–Med |
| `$SYS/#` | broker health: client count, message/byte rates, dropped messages, uptime | Low |
| Arbitrary topic pub/sub | **publish with response capture, subscribe-and-wait, retained-topic dump** — none of this exists in any HA MCP server (HA `mqtt.publish` service is fire-and-forget) | Med |
| Frigate/Zigbee2MQTT topics if present | typed device/event queries over MQTT | Low |

## 8. Execution engines (as tools)
| Engine | Access | Tools | Risk |
|---|---|---|---|
| **AppDaemon** (adjacent tooling present) | write apps to its config dir + REST/reload | deploy Python "apps" for scrapers/custom sensors/watchdogs; the field-tested HA-toolbox pattern | Med |
| **pyscript** (if installed) | `/config/pyscript/`, `pyscript.*` services, `pyscript/reload` | Jupyter-style Python with `@state_trigger`; run arbitrary Python in HA's own process | Med–High |
| **Node-RED** (if add-on) | ingress `/flows` (ha-mcp array_patch covers) | flow CRUD | Med |
| **shell_command / python_script / command_line** | YAML config | register on-box commands as HA services | Med |

## 9. Zigbee (ZHA) deep — beyond §2 WS
- **`zigbee.db`** (SQLite in ZHA config dir): neighbor/route tables → **topology graph & link-quality map** no API renders directly. Read-only. Low risk.
- **Coordinator NVM/backup**: `zha/network/backup` (WS) or radio library → **coordinator migration/DR backup**. Med.
- **Cluster attribute R/W & binding**: WS `zha/devices/clusters/attributes/value` → set manufacturer-specific attributes, direct bindings. Med–High.
- **Channel-energy scan / channel change**: radio ops. High (can drop the mesh).

## 10. go2rtc / Frigate / camera-AI
- **go2rtc** (installed): REST `/api/streams`, `/api/frame.jpeg?src=<cam>` (frame grab), `/api/webrtc`, config. Enables **on-demand frame capture** + stream health as MCP **image content**. Low–Med.
- **Frigate** (person/car image entities present): if the Frigate add-on/API is reachable, `/api/events`, `/api/<cam>/latest.jpg`, `/api/stats`, review/clip queries → typed detection-event tools. Low–Med.
- **LLM Vision + Frigate/go2rtc chaining**: frame → vision analysis pipeline as a single tool. Med.
Access: ingress proxy (ha-mcp-style) or direct container port (needs shared network / add-on).

## 11. Core profiling & debugging
- **`profiler` integration services** (`profiler.start`, `.memory`, `.start_log_objects`, `.dump_log_objects`, `.lru_stats`, `.log_thread_frames`): orchestrate cProfile/objgraph, dump `.prof`/`.hprof` into `/config`, then read the artifact via filesystem → **memory-leak & CPU-hotspot workflow**. Med.
- **debugpy integration**: enable remote debugger attach. Med–High.
- **py-spy / austin** from shell against the Core PID (needs `host_pid` or docker exec into the core container): live flame graphs of a running HA. High.
None exist in any current HA MCP server.

## 12. HACS internals
- `/config/.storage/hacs.*` (repos, data) — read installed/available/pending-update inventory offline. Low.
- HACS WS API: `hacs/repositories`, `hacs/repository` (install/update/delete), `hacs/config` — ha-mcp's `ha_manage_hacs` covers the common path; direct WS gives finer control (download specific version, category filters). Med.

## 13. Bluetooth / dbus / hardware (VM realities)
- **Bluetooth**: WS `bluetooth/subscribe_advertisements` (§2) + `/hardware/info` adapter list. On a **Proxmox VM**, BLE exists only if a USB BT adapter is passed through — `/hardware/info` reveals what the VM actually sees. Low.
- **dbus**: `host_dbus: true` → systemd/NetworkManager/hostname introspection beyond the Supervisor wrapper. Med.
- **USB/serial passthrough visibility**: `/hardware/info` + `/dev/serial/by-id` (mapped) → confirm the Zigbee coordinator's passthrough from the VM, detect flaky USB. Low. **VM caveat:** no bare-metal PCI/thermal/SMART unless Proxmox passes it through; for host-level metrics use the separate Proxmox MCP, not this add-on.

---

## Priority map (surfaces → highest-value NEW tools, vs ha-mcp)
1. **`.storage` registry surgery + dependency/impact graph** (§5, §2) — the killer power-user feature.
2. **Recorder SQL analytics / DB-bloat & purge advisor** (§6) — no API can do it.
3. **Real-time tier: wait_for_state, event-window capture, log-follow, trace-next-run, resident watchdogs** (§2 subscribe_*, §8 AppDaemon) — cross-time capability nothing else has.
4. **MQTT broker layer: discovery audit, $SYS stats, pub/sub-with-response** (§7).
5. **Host/OS/Supervisor deep ops: resolution checks, host services, journald, datadisk, network, freeze/thaw backups, hardware/USB audit** (§3, §4).
6. **Protocol depth: ZHA cluster R/W + topology + coordinator backup** (§9, §2).
7. **Statistics repair (import/adjust), Assist/conversation test harness, blueprint authoring, custom_component scaffold** (§2).
8. **Profiling/debugging workflow** (§11) and **typed camera-AI tools** (§10).

## Cross-cutting risk & manifest note
The most valuable tools need `full_access` **or** the combination `hassio_api:manager` + `homeassistant_api` + broad `map:` + `host_pid`/`host_dbus`. Supervisor restricts some flag combinations (notably `full_access` with fine-grained API roles) and marks such add-ons as protected/reduces the security rating — **the architect must validate the exact manifest against Supervisor 2026.7 at build time**, and the design should gate High-risk tools behind an explicit opt-in (mirroring ha-mcp's Read-Only-Mode / per-tool approval pattern) with mandatory pre-write backups for §5/§6 writes.

### Sources
- REST: https://developers.home-assistant.io/docs/api/rest/
- WebSocket: https://developers.home-assistant.io/docs/api/websocket/ • command gist https://gist.github.com/mhagger/f1cc7844a7736bd5258d953e0a22b398 • DeepWiki recorder/registry: https://deepwiki.com/home-assistant/core/3.1-recorder-and-statistics , https://deepwiki.com/home-assistant/core/2.2-entity-and-registry-management
- Supervisor endpoints (full path inventory verified): https://developers.home-assistant.io/docs/api/supervisor/endpoints/
- Add-on manifest/permissions: https://developers.home-assistant.io/docs/add-ons/configuration/ • https://mantikor.github.io/developers/hassio/addon_config/
- Live ha-mcp tool descriptions (this session) confirmed which of the above are already wrapped vs. gaps.
- **Unverified/version-sensitive** (flagged inline): exact 2026.7 WS command spellings (backup/*, recorder/*, zha/*), recorder DB schema rev, and Supervisor flag-combination rules — all require confirmation against core/Supervisor 2026.7 source before implementation.
