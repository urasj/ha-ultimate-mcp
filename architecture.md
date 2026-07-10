# architecture.md — Ultimate MCP Server for Home Assistant ("umcp")

**Target:** HAOS 2026.7.1, Supervisor-managed, Proxmox VM 100 on `proxmox1` (2 vCPU, 10 GB RAM at ~95% — memory is the scarcest resource). Coexists with `ha-mcp 7.11.0.dev`; our niche is the **system-level** toolset: Supervisor, filesystem, recorder DB, MQTT/ZHA/Z-Wave internals, diagnostics, HACS, dashboards-as-files, checkpoint-before-risk.

---

## 1. Add-on packaging & deploy loop

### 1.1 GitHub repo = add-on repository

Repo `github.com/urasj/ha-ultimate-mcp`, added to the HA add-on store via **Settings → Add-ons → Add-on Store → ⋮ → Repositories → `https://github.com/urasj/ha-ultimate-mcp`**.

**`repository.yaml`** (repo root):

```yaml
name: "Ultimate MCP Server"
url: "https://github.com/urasj/ha-ultimate-mcp"
maintainers:
  - "Justin <justinuras@gmail.com>"
```

**Update loop:** push to `main` → CI builds per-arch images → a release is: bump `version:` in `ultimate-mcp/config.yaml` + entry in `CHANGELOG.md` + git tag `vX.Y.Z`. Supervisor polls the repo (or user hits "Check for updates"); version string change = update offered. That is the entire "push to GitHub → update add-on" loop.

**Critical memory decision — prebuilt images, not on-box builds.** At 95% RAM, letting Supervisor `docker build` on the box risks OOM. Set the `image:` key so Supervisor **pulls** from GHCR instead of building locally:

```yaml
image: "ghcr.io/urasj/{arch}-ultimate-mcp"
```

GitHub Actions (`home-assistant/builder`) builds and pushes `ghcr.io/urasj/amd64-ultimate-mcp:X.Y.Z` (amd64 only is strictly needed for a Proxmox VM, but build aarch64 too for portability).

### 1.2 `ultimate-mcp/config.yaml` (the load-bearing file)

```yaml
name: "Ultimate MCP"
version: "0.1.0"
slug: "ultimate_mcp"
description: "Deep system-level MCP server: Supervisor, filesystem, recorder DB, MQTT/ZHA internals, diagnostics"
url: "https://github.com/urasj/ha-ultimate-mcp"
arch: [amd64, aarch64]
image: "ghcr.io/urasj/{arch}-ultimate-mcp"
init: false                      # s6-overlay from base image manages PID1
homeassistant: "2026.7.0"        # min core version
startup: services
boot: auto

# --- API surfaces ---
hassio_api: true                 # talk to http://supervisor/*
hassio_role: manager             # see tier table below
homeassistant_api: true          # proxy to core REST+WS at http://supervisor/core/api
auth_api: false

# --- Filesystem mounts (2023+ map syntax) ---
map:
  - type: homeassistant_config   # /homeassistant  (== /config of core: yaml, .storage, db, custom_components, www)
    read_only: false
  - type: addon_config           # /config (our own persistent config)
    read_only: false
  - type: all_addon_configs      # /addon_configs/<slug> — deploy into other add-ons (replaces init_commands hack)
    read_only: false
  - type: ssl
    read_only: true
  - type: share
    read_only: false
  - type: media
    read_only: false
  - type: backup                 # /backup — verify checkpoint artifacts exist
    read_only: true

# --- Network: direct port, NOT ingress, NOT host_network ---
host_network: false
ports:
  8099/tcp: 8099
ports_description:
  8099/tcp: "MCP streamable-HTTP endpoint (/mcp)"
ingress: false

# --- Services ---
services:
  - mqtt:want                    # broker creds from Supervisor services API if Mosquitto present

watchdog: "http://[HOST]:[PORT:8099]/health"
apparmor: true                   # custom profile in ultimate-mcp/apparmor.txt

options:
  log_level: info
  auth_token: ""                 # required; server refuses to start if empty
  enabled_surfaces: ["all"]
  destructive_enabled: false     # master kill-switch for T3 tools
schema:
  log_level: "list(debug|info|warning|error)"
  auth_token: "password"
  enabled_surfaces: ["str"]
  destructive_enabled: "bool"
```

**Why not ingress:** ingress requires an authenticated HA browser session and rewrites paths — useless for Cowork/Claude connecting as an MCP client over LAN. Direct mapped port + bearer token is the right transport. **Why not `host_network`:** unnecessary (one port suffices) and it disables port remapping; mDNS/SSDP scanning tools lose some fidelity without it — acceptable trade; if a future `network_discovery` surface needs it, ship it as a separately-flagged option.

**Deliberately NOT requested:** `docker_api` (would force protection-mode-off), `full_access`, `privileged`, `host_pid`, `uart` (never touch the Zigbee serial port — ZHA owns it; all Zigbee work goes through the core WS API). Protection mode stays **ON**.

### 1.3 Permission flags → tool-tier unlock map

| Flag / value | Unlocks (tool surfaces) |
|---|---|
| `homeassistant_api: true` | All core REST/WS tools: states, registries (`config/entity_registry/*` WS), templates, `recorder/*` WS, `zha/*` WS, `zwave_js/*` WS, profiler services, diagnostics downloads |
| `hassio_api: true` + `hassio_role: default` | Read-only Supervisor info endpoints only |
| `hassio_role: homeassistant` | `POST /core/check`, `/core/restart`, `/core/stop`, `/core/start` — config-validate + restart tools |
| `hassio_role: backup` | `POST /backups/new/partial` — the checkpoint engine |
| `hassio_role: manager` **(chosen)** | All of the above **plus** add-on lifecycle (`/addons/<slug>/{options,restart,update,stop,start}`, `/store/*`), `/os/info`, `/host/info`, network info. This is the `ha` CLI equivalent: nearly every `ha ...` command maps 1:1 to a Supervisor REST endpoint, so "HA CLI tools" = Supervisor API calls, no shell needed |
| `hassio_role: admin` (NOT chosen initially) | Host reboot/shutdown, security endpoints. Documented upgrade path only |
| `map: homeassistant_config:rw` | `filesystem/` (all of /config incl. `.storage`), `database/` (SQLite at `/homeassistant/home-assistant_v2.db`), `dashboards/` (`.storage/lovelace*`, YAML dashboards), custom_components inventory, `www/` deploys |
| `map: all_addon_configs:rw` | Direct file deploys into other add-ons (AppDaemon apps etc.) — supersedes the base64-init_commands trick; keep init_commands as documented fallback |
| `map: share:rw`, `media:rw` | `media/` surface, cross-add-on file exchange |
| `map: backup:ro` | Checkpoint verification (stat the `.tar` after creating it) |
| `services: mqtt:want` | `network/mqtt_*` introspection tools — creds via `GET http://supervisor/services/mqtt` |
| `ports: 8099` | The MCP transport itself |

**Flag for 2026.7 doc verification:** exact `map` type names (`homeassistant_config`, `all_addon_configs`, `addon_config`); whether `mqtt:want` vs `mqtt:need` semantics changed; base-image tag (below).

### 1.4 Dockerfile (`ultimate-mcp/Dockerfile`)

```dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.21   # verify current tag vs 2026.7
FROM $BUILD_FROM
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
COPY server /opt/umcp
RUN pip install --no-cache-dir /opt/umcp   # deps: fastmcp, httpx, pydantic, aiomqtt, orjson — NO pandas/numpy
COPY rootfs /                              # /etc/services.d/umcp/run (s6) -> exec python -m ultimate_mcp
HEALTHCHECK CMD wget -qO- http://127.0.0.1:8099/health || exit 1
```

`build.yaml` pins `build_from` per arch. Memory budget: **< 150 MB RSS steady-state** (Alpine + lean deps + lazy imports; add `MALLOC_ARENA_MAX=2`).

---

## 2. Server core

- **Python 3.12+** (3.13 via base image), **FastMCP 2.x**, **streamable HTTP** transport mounted at `http://<ha-ip>:8099/mcp`, plus `/health`. Static bearer-token auth middleware reading `auth_token` from `/data/options.json`. Single `httpx.AsyncClient` for Supervisor (`http://supervisor`, header `Authorization: Bearer ${SUPERVISOR_TOKEN}`) and one persistent WS connection to `ws://supervisor/core/websocket` with auto-reconnect.

- **Lazy tool-module loading.** Package `ultimate_mcp/tools/<surface>/` per surface. Each surface ships a tiny `manifest.py` (pure data: tool names, one-line descs, tier, capability predicates, JSON-schema stubs) that the registry loads at startup; the heavy `impl.py` is imported via `importlib.import_module` **on first invocation** of any tool in that surface. Startup imports ≈ manifest data only → fast boot, low RSS; surfaces the fingerprint disables are never imported at all.

- **150+ tools without blowing client context — search-based discovery.** Flat registration of 150 tools ≈ 40–70k tokens of schemas pushed into every client session; unacceptable, and it double-collides with the coexisting ha-mcp server. Instead expose **~10 real MCP tools**:
  1. `umcp_fingerprint` — the profile (§3)
  2. `umcp_search_tools(query, surface?, tier?)` — BM25-ish search over manifests; returns full JSON schema + annotations (`readOnlyHint`/`destructiveHint`) for matches
  3. `umcp_describe_tool(name)`
  4. `umcp_call(name, args, dry_run=?, confirm_token=?)` — the single dispatcher; validates args against the manifest schema before importing impl
  5. `umcp_checkpoint`, `umcp_undo`, `umcp_journal` — safety kernel (§4), always flat
  6. `umcp_health`, `umcp_enable_surface`

  Same pattern the resident ha-mcp already uses, so clients on this box know the dance. Trade-off accepted: one extra round-trip per new tool vs. ~95% context savings. Internal ("virtual") tools still carry full MCP-style schemas so a future flat-mode flag (`expose_flat: ["database", ...]`) can promote hot surfaces to real registrations.

---

## 3. Fingerprint module (`ultimate_mcp/fingerprint/`)

Runs at startup, cached to `/data/fingerprint.json`, refreshed on demand (`umcp_fingerprint(refresh=true)`) and on Supervisor `addon`/`core` update events. Sections and sources:

| Section | Source |
|---|---|
| `core` (version, config dir, safe_mode, timezone) | `GET /api/config`; `GET http://supervisor/core/info` |
| `supervisor`, `os`, `host` (HAOS, VM, disk) | `GET http://supervisor/{supervisor,os,host}/info` |
| `hardware` | `GET http://supervisor/hardware/info` (KVM/Proxmox confirmation, USB-passthrough radios) |
| `addons` (installed, versions, running, ports) | `GET http://supervisor/addons` |
| `integrations` / config entries | WS `config_entries/get` + `config/entity_registry/list` counts per domain |
| `custom_components` | `os.scandir('/homeassistant/custom_components')` + each `manifest.json` |
| `hacs` | `/homeassistant/.storage/hacs.repositories` + `hacs.data` (read-only parse); fallback WS `hacs/repositories/list` — **verify command name vs current HACS** |
| `database` | recorder WS `recorder/info`; engine sniff via `recorder:` YAML block else SQLite; `stat()` for size; `PRAGMA page_count` |
| `recorder settings` | purge_keep_days, include/exclude from parsed YAML |
| `network` | `GET http://supervisor/network/info` |
| `breaking_changes` | `GET https://version.home-assistant.io/stable.json`; diff installed integrations against release-notes breaking-change sections — best-effort, cached 24h, offline-tolerant |

**Capability gating contract:** every manifest declares `requires`, e.g. `requires: ["integration:zha"]`, `["addon:core_mosquitto", "service:mqtt"]`, `["db:sqlite"]`. The registry evaluates predicates against the fingerprint; failing tools are excluded from search results (with `unavailable_reason` visible via `describe_tool`). On this box: Z-Wave tools auto-disabled, ZHA/MQTT/HACS/go2rtc surfaces enabled, DB tools pinned to SQLite mode.

---

## 4. Safety model

**Tiers** (per-tool in manifests, surfaced as MCP annotations):

- **T0 read** — no side effects; always allowed.
- **T1 write-reversible** — registry renames, helper edits, MQTT publish to test topics; requires `dry_run=false` explicitly.
- **T2 write-risky** — YAML/dashboard/.storage edits, add-on option changes, DB non-SELECT, core restart; requires prior **checkpoint** in the same session + dry-run preview.
- **T3 destructive** — purge DB, delete add-on, remove config entries, raw SQL writes; requires `destructive_enabled: true` in add-on options **and** a `confirm_token` minted by the dry-run response (two-call handshake), **and** checkpoint.

**Dry-run everywhere:** every mutating tool takes `dry_run` (default `true`) and returns a structured diff/plan (`{would_change: [...], checkpoint_required: "partial:homeassistant", confirm_token}`).

**Checkpoints:** `umcp_checkpoint(scope)` → `POST http://supervisor/backups/new/partial` scoped to the blast radius; verify the tarball appears under `/backup`. Journal records backup slug. **Proxmox escalation:** the server must not hold PVE creds; T3 dry-run responses include `external_checkpoint_hint: {type:"proxmox_snapshot", node:"proxmox1", vmid:100}` — the MCP *client* (Cowork, which has a Proxmox MCP) takes the snapshot and passes `external_checkpoint_ref` back with the confirm call; the journal stores it.

**Validate-before-restart:** any tool whose plan includes core restart runs `POST http://supervisor/core/check` first and aborts on failure (returning the error log excerpt).

**.storage edit protocol** (single implementation in `safety/storage_editor.py`, all registry/dashboard tools route through it):
1. partial backup (`homeassistant: true`)
2. `POST /core/stop`
3. copy target file to `/data/undo/<ts>/`
4. edit as tmp-file + `os.replace` (atomic)
5. `json.loads` re-validate + schema sanity (`version`/`key` fields intact)
6. `POST /core/start`
7. verify via WS (entity present / dashboard loads)
8. journal entry.
Timeout guard: if core doesn't come back in 120s, auto-restore the copy and start again.

**Undo journal:** append-only JSONL at `/data/journal.jsonl` + per-change artifacts in `/data/undo/`; `umcp_undo(entry_id)` replays inverse ops. Cross-cutting gotchas from the toolbox skill baked in: add-on option changes apply only after restart (tools chain it), shell payloads > ~128 KB chunked (MAX_ARG_STRLEN), `/local/` serving requires pre-existing `www/`.

---

## 5. Repo layout

```
ha-ultimate-mcp/
├── repository.yaml
├── ultimate-mcp/                  # the add-on dir (what Supervisor reads)
│   ├── config.yaml  Dockerfile  build.yaml  apparmor.txt
│   ├── CHANGELOG.md  DOCS.md  README.md  icon.png  logo.png
│   └── rootfs/etc/services.d/umcp/run
├── server/                        # pip-installable package
│   ├── pyproject.toml
│   └── ultimate_mcp/
│       ├── __main__.py  app.py         # FastMCP wiring, auth, /health
│       ├── registry.py                 # manifest loader, search, lazy dispatch
│       ├── context.py                  # SupervisorClient, HaWsClient, FsFacade, DbFacade
│       ├── fingerprint/{collect.py, gates.py, breaking.py}
│       ├── safety/{tiers.py, checkpoint.py, storage_editor.py, journal.py, dryrun.py}
│       └── tools/
│           ├── supervisor/{manifest.py, impl.py}     # addons, core, os, host, store, "ha CLI" parity
│           ├── filesystem/…      # /config CRUD, .storage (via safety), yaml lint, grep
│           ├── database/…        # ro-SQL (sqlite URI mode=ro + PRAGMA query_only), stats, purge (T3)
│           ├── registries/…      # entity/device/area/label via WS
│           ├── network/…         # mqtt introspection (aiomqtt via services API), net info
│           ├── zigbee/…          # zha/* WS: devices, neighbors, bind, cluster attrs  [verify cmd names]
│           ├── diagnostics/…     # profiler.* services, diagnostics downloads, log tail, resolution center
│           ├── hacs/…            # inventory, update, pending-updates diff
│           ├── dashboards/…      # lovelace .storage↔YAML, card lint, headless screenshot hook
│           └── media/…           # /media, /share, go2rtc probe
├── tests/
│   ├── fixtures/                  # recorded Supervisor/WS JSON (respx + replay harness)
│   ├── unit/  integration/
│   └── conftest.py                # fake fingerprint profiles: this_box.json, no_zha.json, mariadb.json
└── .github/workflows/{ci.yaml, build.yaml, release.yaml}
    # ci: ruff + mypy + pytest | build: home-assistant/builder → GHCR per-arch | release: tag gate = config.yaml version match
```

---

## 6. Team-of-agents build plan (6 workstreams)

**Stable contract first (W0 blocks everyone):** `registry.py` + `context.py` + manifest dataclasses (`ToolSpec{name, tier, requires, schema, summary}`) + `safety/` kernel + fingerprint stub returning a canned `this_box.json`. Every other workstream codes against `Context` facades only (never raw HTTP) so recorded-fixture tests work uniformly.

| WS | Owner scope | Depends on | Interface consumed |
|---|---|---|---|
| W0 | Core: app, registry, context, safety, fingerprint, search | — | defines everything |
| W1 | `supervisor/` + `hacs/` | W0 | `ctx.supervisor.get/post`, checkpoint API |
| W2 | `filesystem/` + `dashboards/` + `.storage` flows | W0 | `ctx.fs`, `storage_editor` |
| W3 | `database/` + `diagnostics/` | W0 | `ctx.db`, `ctx.ha_ws.call` |
| W4 | `network/` (MQTT) + `zigbee/` (+ zwave stubs, gated off) | W0 | `ctx.mqtt`, `ctx.ha_ws` |
| W5 | `registries/` + `media/` | W0 | `ctx.ha_ws`, `ctx.fs` |
| W6 | Packaging/CI: add-on dir, Dockerfile, builder workflow, release gating, on-box smoke test | W0 API frozen | — |

Merge order: W0 → (W1–W5 in parallel, each PR = one surface: manifest + impl + fixtures + tests) → W6 release `0.1.0`. Definition of done per tool: schema-validated, tiered, dry-run implemented if mutating, gated by `requires`, ≥1 recorded-fixture test, appears in `umcp_search_tools`.

**Items flagged for verification against 2026.7 docs:** base-image tag; `map:` long-form type names; `hassio_role` capability matrix (esp. which role gates `/core/stop`); `zha/*` and `hacs/*` WS command names; `backups/new/partial` body schema; whether streamable-HTTP FastMCP needs `stateless_http=True` for multi-client LAN use.

### Critical files for implementation
- `server/ultimate_mcp/registry.py` — manifest loading, search-based discovery, lazy dispatch (the core contract)
- `server/ultimate_mcp/context.py` — Supervisor/WS/FS/DB facades every tool module depends on
- `ultimate-mcp/config.yaml` — permission flags, ports, options; gates every tool tier
- `server/ultimate_mcp/safety/storage_editor.py` — the stop-copy-edit-verify protocol all risky writes route through
- `server/ultimate_mcp/fingerprint/collect.py` — installation profile that drives capability gating
- `.github/workflows/build.yaml` — per-arch GHCR image build enabling the push-to-update loop
