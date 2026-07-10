"""diagnostics/ surface manifest — log triage, repairs, logger, profiler (W4b).

Pure data. No surface-level gate (every HA install has system_log/logger/repairs);
the profiler/debugpy T2 tools individually require their optional integration.
Reads route through ctx.ha_ws.call; service calls (logger/profiler/debugpy)
route through ctx.supervisor.core_api("POST", "services/<domain>/<service>", ...).
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_DRY_RUN = {"type": "boolean", "default": True, "description": "Preview only (default true)"}
_LOG_LEVELS = ["debug", "info", "warning", "error", "critical", "fatal", "notset"]

SURFACE = SurfaceSpec(
    name="diagnostics",
    summary="System-log triage, integration diagnostics, startup timing, repair issues, "
    "per-integration log levels, and the CPU/memory profiler workflow",
    impl_module="ultimate_mcp.tools.diagnostics.impl",
    requires=(),  # no gate: log/logger/repairs exist on every install
    tools=(
        # ------------------------------------------------------------- T0 reads
        ToolSpec(
            name="system_log_triage",
            summary="Fetch system_log/list and cluster errors/warnings by integration + level",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "min_level": {
                        "type": "string",
                        "enum": ["warning", "error", "critical"],
                        "default": "warning",
                        "description": "Only include entries at this level or worse",
                    }
                },
            },
            keywords=("logs", "errors", "warnings", "triage", "cluster", "system_log", "health"),
        ),
        ToolSpec(
            name="integration_diagnostics",
            summary="Download the diagnostics dump for a config entry (integration)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "config_entry_id": {
                        "type": "string",
                        "description": "config_entries entry_id to dump diagnostics for",
                    }
                },
                "required": ["config_entry_id"],
            },
            keywords=("diagnostics", "dump", "integration", "config_entry", "download", "support"),
        ),
        ToolSpec(
            name="startup_time_report",
            summary="Per-integration setup/startup times (slowest integrations first)",
            tier=Tier.T0_READ,
            keywords=("startup", "boot", "setup", "slow", "timing", "performance", "time"),
        ),
        ToolSpec(
            name="repairs_list",
            summary="List active repair issues (repairs/list_issues) with severity and domain",
            tier=Tier.T0_READ,
            keywords=("repairs", "issues", "problems", "fix", "warnings", "list"),
        ),
        ToolSpec(
            name="logger_levels",
            summary="Current logger configuration + effective per-integration levels (logger/log_info)",
            tier=Tier.T0_READ,
            keywords=("logger", "log", "levels", "debug", "verbosity", "config"),
        ),
        # --------------------------------------------------------- T1 reversible
        ToolSpec(
            name="logger_set_level",
            summary="Set the log level for one integration via logger.set_level (T1); "
            "dry_run shows the intended service call",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "integration": {
                        "type": "string",
                        "description": "Domain or logger name, e.g. 'zha' or 'homeassistant.components.zha'",
                    },
                    "level": {"type": "string", "enum": _LOG_LEVELS},
                    "dry_run": _DRY_RUN,
                },
                "required": ["integration", "level"],
            },
            keywords=("logger", "set", "level", "debug", "toggle", "verbosity"),
        ),
        ToolSpec(
            name="repairs_ignore",
            summary="Ignore (or un-ignore) a repair issue via repairs/ignore_issue (T1)",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "domain": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Owning integration domain (looked up from repairs_list if omitted)",
                    },
                    "ignore": {"type": "boolean", "default": True},
                    "dry_run": _DRY_RUN,
                },
                "required": ["issue_id"],
            },
            keywords=("repairs", "ignore", "dismiss", "issue", "silence"),
        ),
        # --------------------------------------------------------------- T2 risky
        ToolSpec(
            name="profiler_start",
            summary="Start a cProfile capture via profiler.start; writes a .prof into /config "
            "(T2). dry_run notes the artifact it would create",
            tier=Tier.T2_RISKY,
            requires=("integration:profiler",),
            schema={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "default": 60,
                        "minimum": 1,
                        "maximum": 3600,
                        "description": "Capture duration passed to profiler.start",
                    },
                    "dry_run": _DRY_RUN,
                },
            },
            keywords=("profiler", "cprofile", "cpu", "hotspot", "prof", "performance", "capture"),
        ),
        ToolSpec(
            name="profiler_memory",
            summary="Capture a memory snapshot via profiler.memory; writes an .hpy/.hprof into "
            "/config (T2). dry_run notes the artifact it would create",
            tier=Tier.T2_RISKY,
            requires=("integration:profiler",),
            schema={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "default": 60,
                        "minimum": 1,
                        "maximum": 3600,
                        "description": "Capture duration passed to profiler.memory",
                    },
                    "dry_run": _DRY_RUN,
                },
            },
            keywords=("profiler", "memory", "leak", "objgraph", "heap", "snapshot", "hprof"),
        ),
        ToolSpec(
            name="debugpy_enable",
            summary="Enable the debugpy remote debugger (debugpy.start) so a debugger can attach "
            "(T2). dry_run notes the port it would open",
            tier=Tier.T2_RISKY,
            requires=("integration:debugpy",),
            schema={"type": "object", "properties": {"dry_run": _DRY_RUN}},
            keywords=("debugpy", "debugger", "remote", "attach", "vscode", "breakpoint"),
        ),
    ),
)
