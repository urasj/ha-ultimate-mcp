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
    ),
)
