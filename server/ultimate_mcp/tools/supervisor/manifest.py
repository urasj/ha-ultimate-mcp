"""supervisor/ surface manifest — Supervisor REST control plane (W1).

Pure data, loaded at startup. Always present on HAOS, so no surface-level gate.
Add-on-specific tools take a `slug`; every mutating tool carries `dry_run` and is
tier-gated (T1 reversible, T2 risky/checkpoint, T3 destructive) by the dispatcher.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_SLUG = {"type": "string", "description": "Add-on slug, e.g. core_mosquitto"}
_DRY_RUN = {"type": "boolean", "default": True, "description": "Preview only (default true)"}

SURFACE = SurfaceSpec(
    name="supervisor",
    summary="Supervisor control plane: add-on lifecycle, core/OS/host info, resolution "
    "center, jobs, updates, network, backups — read + guarded mutation",
    impl_module="ultimate_mcp.tools.supervisor.impl",
    requires=(),  # Supervisor is always present on HAOS
    tools=(
        # ------------------------------------------------------------ T0 reads
        ToolSpec(
            name="addon_list",
            summary="List installed add-ons with slug, name, version, and state",
            tier=Tier.T0_READ,
            keywords=("addons", "installed", "list", "supervisor", "inventory"),
        ),
        ToolSpec(
            name="addon_info",
            summary="Full add-on info: version, options schema, state, url, boot, ingress",
            tier=Tier.T0_READ,
            schema={"type": "object", "properties": {"slug": _SLUG}, "required": ["slug"]},
            keywords=("addon", "info", "detail", "version", "options", "config"),
        ),
        ToolSpec(
            name="addon_stats",
            summary="Add-on runtime stats: CPU %, memory usage/limit, network/disk IO",
            tier=Tier.T0_READ,
            schema={"type": "object", "properties": {"slug": _SLUG}, "required": ["slug"]},
            keywords=("addon", "stats", "cpu", "memory", "resource", "usage"),
        ),
        ToolSpec(
            name="addon_logs",
            summary="Tail the last N lines of an add-on's log",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "slug": _SLUG,
                    "tail": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
                },
                "required": ["slug"],
            },
            keywords=("addon", "logs", "tail", "output", "stderr", "debug"),
        ),
        ToolSpec(
            name="core_info",
            summary="Home Assistant Core info: version, state, machine, ip, audio, ssl",
            tier=Tier.T0_READ,
            keywords=("core", "info", "version", "homeassistant", "state"),
        ),
        ToolSpec(
            name="core_stats",
            summary="Home Assistant Core runtime stats: CPU %, memory, IO",
            tier=Tier.T0_READ,
            keywords=("core", "stats", "cpu", "memory", "resource"),
        ),
        ToolSpec(
            name="os_info",
            summary="Home Assistant OS info: version, board, boot slots, data disk",
            tier=Tier.T0_READ,
            keywords=("os", "haos", "operating", "system", "board", "version"),
        ),
        ToolSpec(
            name="host_info",
            summary="Host info: hostname, OS, kernel, CPU, memory, disk, features",
            tier=Tier.T0_READ,
            keywords=("host", "info", "hostname", "kernel", "hardware", "system"),
        ),
        ToolSpec(
            name="host_disk_usage",
            summary="Host disk usage: total/used/free GB and eMMC life-time wear",
            tier=Tier.T0_READ,
            keywords=("disk", "storage", "usage", "free", "space", "wear", "emmc"),
        ),
        ToolSpec(
            name="resolution_report",
            summary="Resolution center: unhealthy/unsupported reasons, issues, suggestions, checks",
            tier=Tier.T0_READ,
            keywords=("resolution", "issues", "unhealthy", "unsupported", "suggestions", "health"),
        ),
        ToolSpec(
            name="jobs_list",
            summary="Supervisor background jobs tree with progress and stage",
            tier=Tier.T0_READ,
            keywords=("jobs", "tasks", "background", "progress", "queue"),
        ),
        ToolSpec(
            name="update_inventory",
            summary="Everything updatable in one view: core, os, supervisor, add-ons",
            tier=Tier.T0_READ,
            keywords=("updates", "available", "upgrade", "pending", "inventory", "outdated"),
        ),
        ToolSpec(
            name="network_info",
            summary="Supervisor network info: interfaces, IPv4/IPv6, DNS, connectivity",
            tier=Tier.T0_READ,
            keywords=("network", "interface", "ip", "dns", "connectivity", "ethernet"),
        ),
        # ------------------------------------------------------- T1 reversible
        ToolSpec(
            name="addon_options_set",
            summary="Merge & write add-on options (optionally restart); dry_run shows merged options",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "slug": _SLUG,
                    "options": {"type": "object", "description": "Keys to merge over current options"},
                    "restart": {"type": "boolean", "default": False, "description": "Restart add-on after write"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["slug", "options"],
            },
            keywords=("addon", "options", "config", "set", "configure", "restart"),
        ),
        ToolSpec(
            name="resolution_apply_suggestion",
            summary="Apply a resolution-center suggestion by uuid (e.g. clear an issue)",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "uuid": {"type": "string", "description": "Suggestion uuid from resolution_report"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["uuid"],
            },
            keywords=("resolution", "suggestion", "apply", "fix", "issue"),
        ),
        # ------------------------------------------------------------ T2 risky
        ToolSpec(
            name="addon_restart",
            summary="Restart an add-on",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {"slug": _SLUG, "dry_run": _DRY_RUN},
                "required": ["slug"],
            },
            keywords=("addon", "restart", "reboot", "bounce"),
        ),
        ToolSpec(
            name="addon_update",
            summary="Update an add-on to the latest (or a given) version",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "slug": _SLUG,
                    "version": {"type": ["string", "null"], "default": None, "description": "Target version (omit for latest)"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["slug"],
            },
            keywords=("addon", "update", "upgrade", "version"),
        ),
        ToolSpec(
            name="addon_start",
            summary="Start an add-on",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {"slug": _SLUG, "dry_run": _DRY_RUN},
                "required": ["slug"],
            },
            keywords=("addon", "start", "run", "launch"),
        ),
        ToolSpec(
            name="addon_stop",
            summary="Stop an add-on",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {"slug": _SLUG, "dry_run": _DRY_RUN},
                "required": ["slug"],
            },
            keywords=("addon", "stop", "halt", "shutdown"),
        ),
        ToolSpec(
            name="core_restart",
            summary="Restart Home Assistant Core — always preceded by a config check that aborts on invalid",
            tier=Tier.T2_RISKY,
            schema={"type": "object", "properties": {"dry_run": _DRY_RUN}},
            keywords=("core", "restart", "reboot", "check", "homeassistant"),
        ),
        ToolSpec(
            name="backup_partial",
            summary="Create a partial backup (choose add-ons/folders and whether to include HA core)",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"], "default": None},
                    "homeassistant": {"type": "boolean", "default": True},
                    "addons": {"type": "array", "items": {"type": "string"}, "default": []},
                    "folders": {"type": "array", "items": {"type": "string"}, "default": []},
                    "dry_run": _DRY_RUN,
                },
            },
            keywords=("backup", "partial", "snapshot", "save"),
        ),
        ToolSpec(
            name="backup_full",
            summary="Create a full backup of the whole instance",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"], "default": None},
                    "dry_run": _DRY_RUN,
                },
            },
            keywords=("backup", "full", "snapshot", "save", "complete"),
        ),
        ToolSpec(
            name="store_repo_add",
            summary="Add an add-on store repository by URL",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "Repo URL, e.g. https://github.com/user/repo"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["repository"],
            },
            keywords=("store", "repository", "repo", "add", "source"),
        ),
        # ----------------------------------------------------- T3 destructive
        ToolSpec(
            name="addon_uninstall",
            summary="Uninstall an add-on (destructive — removes container and config)",
            tier=Tier.T3_DESTRUCTIVE,
            schema={
                "type": "object",
                "properties": {"slug": _SLUG, "dry_run": _DRY_RUN},
                "required": ["slug"],
            },
            keywords=("addon", "uninstall", "remove", "delete", "destroy"),
        ),
    ),
)
