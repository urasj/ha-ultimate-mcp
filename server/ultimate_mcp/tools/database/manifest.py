"""database/ surface manifest — pure data, loaded at startup (reference surface for W3)."""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "SELECT-only SQL"},
        "limit": {"type": "integer", "default": 200, "maximum": 2000},
    },
    "required": ["sql"],
}

SURFACE = SurfaceSpec(
    name="database",
    summary="Direct recorder DB analytics: bloat audits, entity storage cost, purge previews",
    impl_module="ultimate_mcp.tools.database.impl",
    requires=("db:sqlite",),  # MariaDB variant gated separately in W3
    tools=(
        ToolSpec(
            name="db_query",
            summary="Run read-only SQL against the recorder database (mode=ro, query_only)",
            tier=Tier.T0_READ,
            schema=_SQL_SCHEMA,
            keywords=("sql", "select", "recorder", "sqlite", "history"),
        ),
        ToolSpec(
            name="db_schema",
            summary="Recorder schema: tables, columns, schema_changes revision",
            tier=Tier.T0_READ,
            keywords=("tables", "columns", "migration", "version"),
        ),
        ToolSpec(
            name="db_size_report",
            summary="Database size, page stats, and per-table row counts",
            tier=Tier.T0_READ,
            keywords=("bloat", "disk", "size", "big", "storage"),
        ),
        ToolSpec(
            name="db_entity_cost",
            summary="Top entities by recorder row count — what is bloating the DB",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"top": {"type": "integer", "default": 25}},
            },
            keywords=("noisy", "churn", "rows", "cost", "exclude", "recorder"),
        ),
        ToolSpec(
            name="db_purge_preview",
            summary="Preview what recorder.purge would delete for given keep-days",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"keep_days": {"type": "integer", "default": 10}},
            },
            keywords=("purge", "cleanup", "delete", "preview", "dry"),
        ),
        ToolSpec(
            name="db_churn_top",
            summary="Entities writing the most state rows in a recent window (recorder churn)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "default": 24, "minimum": 1},
                    "top": {"type": "integer", "default": 25, "maximum": 200},
                },
            },
            keywords=("churn", "noisy", "chatty", "spam", "window", "recent", "writes"),
        ),
        ToolSpec(
            name="db_event_firehose",
            summary="Event types by volume in a recent window — which integrations flood the bus",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "default": 24, "minimum": 1},
                    "top": {"type": "integer", "default": 25, "maximum": 200},
                },
            },
            keywords=("events", "firehose", "bus", "volume", "noisy", "event_type"),
        ),
        ToolSpec(
            name="db_stats_gaps",
            summary="Gaps and NULL runs in long-term statistics (missing hourly rows)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "statistic_id": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Limit to one statistic_id, e.g. sensor.energy_total",
                    },
                    "days": {"type": "integer", "default": 7, "minimum": 1},
                },
            },
            keywords=("statistics", "gaps", "missing", "hourly", "null", "anomaly", "ltss"),
        ),
        ToolSpec(
            name="db_attr_bloat",
            summary="Largest shared attribute payloads in state_attributes with usage counts",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"top": {"type": "integer", "default": 25, "maximum": 200}},
            },
            keywords=("attributes", "bloat", "payload", "shared_attrs", "json", "large"),
        ),
        ToolSpec(
            name="db_recorder_advisor",
            summary="Recommend recorder exclude candidates from cost+churn with a ready-to-paste YAML block",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "threshold_rows": {
                        "type": "integer",
                        "default": 10000,
                        "description": "Entities with at least this many state rows become exclude candidates",
                    }
                },
            },
            keywords=("advisor", "exclude", "recorder", "yaml", "savings", "recommend", "tune"),
        ),
        ToolSpec(
            name="db_integrity_check",
            summary="PRAGMA integrity_check plus freelist and page statistics",
            tier=Tier.T0_READ,
            keywords=("integrity", "corrupt", "pragma", "freelist", "pages", "health", "check"),
        ),
        ToolSpec(
            name="db_restart_history",
            summary="Recorder run history (start/end/closed_incorrectly) — crash and restart forensics",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20, "maximum": 200}},
            },
            keywords=("restarts", "recorder_runs", "crash", "unclean", "shutdown", "history"),
        ),
        ToolSpec(
            name="db_purge_execute",
            summary="Execute recorder.purge / recorder.purge_entities via the HA service (never raw DELETE)",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "keep_days": {"type": "integer", "minimum": 0},
                    "repack": {"type": "boolean", "default": False},
                    "entity_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "default": None,
                        "description": "If set, purge only these entities via recorder.purge_entities",
                    },
                    "dry_run": {"type": "boolean", "default": True},
                },
                "required": ["keep_days"],
            },
            keywords=("purge", "execute", "delete", "repack", "vacuum", "cleanup", "service"),
        ),
    ),
)
