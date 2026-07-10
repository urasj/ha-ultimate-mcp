# Ultimate MCP — Playbook: What To Actually Do With It

Companion to `BLUEPRINT.md` (tool catalog) and `docs/ha-poweruser-research.md` (sourced research).
Target box: HAOS 2026.7 on Proxmox VM 100 (`proxmox1`), 1,152 entities / 39 domains, SQLite recorder,
ZHA, Mosquitto, HACS, go2rtc + Frigate, LLM Vision, HA Cloud.

---

## 0. The safety protocol (hardwired into every significant change)

Nothing that changes how HA operates runs without a rollback path. Three layers, escalating with risk:

1. **T0/T1 (read + reversible)** — no backup needed; reversible writes are journaled with undo.
2. **T2 (risky: YAML/.storage/DB/add-on/core-restart)** — the safety kernel **refuses to run without a
   same-session checkpoint first**: a Supervisor *partial backup* (`umcp_checkpoint`), verified on `/backup`.
   Dry-run shows the exact diff before anything writes.
3. **T3 (destructive) + anything structural** — partial backup **plus a Proxmox VM snapshot of VM 100**
   (full-machine rollback). I take this via the Proxmox MCP before the work starts. VM 100 currently has
   **0 snapshots** — the first structural project starts by creating one.

Rule of thumb we'll follow: *if it edits `.storage`, changes `configuration.yaml` load-bearing blocks,
touches the DB, or restarts core → snapshot first, then checkpoint, then dry-run, then execute, then verify.*

---

## 1. Guided projects (we drive the tools live, one at a time)

### Project A — Whole-house DB & health audit → recorder tuning  *(flagship, start here)*
**Why:** DB bloat is the #1 HA performance killer. A real 2026 teardown cut writes >70% (160→<50 MB/day)
just by excluding noise. Your 1,152 entities almost certainly have heavy hitters.

**Steps / tools:**
1. `umcp_call db_size_report` + `db_entity_cost` + `db_churn_top` + `db_attr_bloat` — rank what's actually
   bloating the recorder (high-churn entities, giant shared_attrs, the `update` domain, permanently-`unavailable`
   entities that still write rows).
2. `db_recorder_advisor` — get a ready-to-paste `recorder:` exclude block with projected row savings.
3. Review together → apply via `yaml_edit_any` (T2: snapshot + checkpoint + dry-run diff first).
4. Set `commit_interval: 30` (from default 1s — big I/O cut) and a sane `purge_keep_days` (5–14).
5. `db_purge_execute repack:true` — the VACUUM step everyone forgets; actually shrinks the file.
6. `db_restart_history` + re-run `db_size_report` a day later to confirm the drop.

**Outcome:** smaller, faster DB; a documented recorder policy. **Decision point:** if still huge, plan
SQLite→MariaDB (dedicated LXC on proxmox1; utf8mb4; MariaDB ≥10.6.9).

### Project B — Entity & registry cleanup
**Why:** 1000+ entity installs accumulate orphans, `_2`/`_3` suffix creep, and `device_id` references in
automations that break on device replacement.

**Steps / tools:**
1. `storage_orphan_scan` — registry entries whose device/config-entry is gone (true orphans).
2. `dependency_graph <entity_id>` — before renaming anything, see everywhere it's referenced
   (automations, scripts, dashboards, groups, templates).
3. `entity_rename_deep` (T2) — bulk rename toward `area_device_function`, rewriting every reference,
   dry-run first. Snapshot + checkpoint enforced.
4. `storage_orphan_clean` (T2) — remove confirmed orphans (safer than Spook: full undo + dry-run).
5. Sweep automations off `device_id` → `entity_id` using the dependency graph.

**Outcome:** clean, consistently-named registry; automations that survive device swaps.

### Project C — Zigbee mesh health + coordinator DR
**Why:** concrete thresholds exist and a 100+ device mesh degrades silently.

**Steps / tools:**
1. `zha_topology_graph` — link-quality map; flag every node under threshold and every single-parent node.
   Healthy: LQI >100 (>150 for locks); RSSI better than −70 dBm; problematic below −90.
2. `zha_devices` / `zha_device_detail` — find flaky/weak-signal devices; check router:end-device ratio
   (~1 router per 2–3 end devices, dispersed).
3. `zha_coordinator_backup` (T2) — NVM/network backup *before* any stick or channel change.
4. Trend LQI over weeks via a scheduled snapshot (the live ZHA graph can't do history).

**Outcome:** a map of weak points, placement guidance, and a DR backup of the coordinator.

---

## 2. Autopilot (scheduled tasks that run and report on their own)

Each is a scheduled task calling the relevant tools and pushing a notification/summary.

- **Nightly "house vitals"** — `db_size_report` + `state_flatline_scan` (dead/frozen sensors) +
  supervisor `resolution_report` + add-on/disk health, in one digest. Frozen sensors (online but stale
  `last_updated`) are the sneaky failure mode plain "unavailable" checks miss.
- **Weekly DB audit** — `db_churn_top` + `db_recorder_advisor`; alert if DB grew past a threshold or a new
  noisy entity appeared.
- **Battery sweep** — entities under ~20%, weekly.
- **Add-on / integration crash watch** — supervisor + repairs; alert on new failures.
- **Zigbee trend** — weekly `zha_topology_graph` snapshot to catch slow degradation.

Cadence and thresholds are yours to tune; I'll wire them as scheduled tasks.

---

## 3. Customization menu (the "neat setups" — build + maintain with deep tooling)

Standout 2026 power-user setups our tools make easier to build *and* keep working:

- **Frigate + LLM Vision AI notification timeline** — the marquee 2026 setup: camera detections →
  LLM description → actionable mobile notification + a timeline calendar. Tools: `media_camera` (go2rtc
  frames, snapshots, `llm_vision_analyze`), realtime event capture, dashboards-as-files.
- **Room presence (Bermuda BLE / mmWave)** — ESP32 BLE proxies → per-room presence → presence-driven
  automations. Tools: registries (entity/area org), realtime (test triggers), dependency graph.
- **Adaptive / circadian lighting under presence** — HACS Adaptive Lighting gated by presence. Tools:
  hacs inventory, registries, realtime testing.
- **Energy cost tracking + statistics repair** — time-of-use cost sensors; and when the energy dashboard
  breaks (the classic HA pain), `stats_repair` (`stats_anomaly_scan`, `stats_import`, `stats_adjust_sum`)
  fixes the long-term statistics *no other tool can*.
- **Assist / voice pipeline tuning** — `assist` surface tests your Google AI agents and pipelines,
  diffs responses, lints entity exposure.

We'll pick from this list as we go; each build gets the same backup protocol when it writes to HA.

---

## 4. Suggested order

1. **Project A (DB/health audit)** — fastest, highest-impact, low risk, great showcase.
2. Stand up the **nightly vitals** autopilot (so we're monitoring while we work).
3. **Project B (entity cleanup)** — bigger blast radius; snapshot-gated.
4. **Project C (Zigbee)** + pick a **customization** to build.

Say the word on which to start and I'll drive it live in Claude Desktop — snapshot and checkpoint first,
dry-run shown before anything writes.
