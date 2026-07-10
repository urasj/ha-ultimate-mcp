"""stats_repair/ surface manifest — long-term statistics repair/import (W5).

Pure data. Every tool drives the recorder statistics WebSocket commands via
ctx.ha_ws.call; only stats_anomaly_scan reads the recorder DB directly, so it
carries a per-tool db:sqlite gate. The surface gate is integration:recorder
(the statistics commands live in the recorder integration), NOT db:sqlite —
statistics are edited over WS, never by raw SQL writes.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_STATISTIC_ID = {
    "type": "string",
    "description": "e.g. sensor.energy_total or the external:<...> id",
}
_DRY_RUN = {"type": "boolean", "default": True, "description": "Preview only (default true)"}

SURFACE = SurfaceSpec(
    name="stats_repair",
    summary="Long-term statistics repair: list/inspect statistic ids, scan for anomalies "
    "(spikes/negatives/unit issues), import corrected stats, adjust broken energy sums, "
    "change units, clear statistics — the energy-data fixes nothing else offers",
    impl_module="ultimate_mcp.tools.stats_repair.impl",
    requires=("integration:recorder",),
    tools=(
        ToolSpec(
            name="stats_list",
            summary="List long-term statistic ids (recorder/list_statistic_ids)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "statistic_type": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Filter: 'mean', 'sum', or null for all",
                    }
                },
            },
            keywords=("statistics", "list", "ids", "energy", "long-term", "ltss", "recorder"),
        ),
        ToolSpec(
            name="stats_metadata",
            summary="Statistics metadata: unit, source, has_mean/has_sum "
            "(recorder/get_statistics_metadata)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "statistic_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "default": None,
                        "description": "Limit to these ids (null = all)",
                    }
                },
            },
            keywords=("statistics", "metadata", "unit", "source", "has_sum", "has_mean"),
        ),
        ToolSpec(
            name="stats_info",
            summary="Recorder statistics engine info (recorder/info)",
            tier=Tier.T0_READ,
            keywords=("recorder", "info", "backlog", "migration", "statistics", "status"),
        ),
        ToolSpec(
            name="stats_anomaly_scan",
            summary="Scan long-term statistics for spikes, negatives, non-monotonic energy sums "
            "and missing units (direct recorder DB read)",
            tier=Tier.T0_READ,
            requires=("db:sqlite",),
            schema={
                "type": "object",
                "properties": {
                    "statistic_id": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Limit to one statistic_id (null = all)",
                    },
                    "days": {"type": "integer", "default": 30, "minimum": 1},
                    "spike_factor": {
                        "type": "number",
                        "default": 10.0,
                        "minimum": 2.0,
                        "description": "Flag a mean that jumps by more than this multiple",
                    },
                },
            },
            keywords=("anomaly", "spike", "negative", "outlier", "reset", "unit", "energy", "scan"),
        ),
        ToolSpec(
            name="stats_import",
            summary="Import/overwrite corrected statistics (recorder/import_statistics)",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "metadata": {
                        "type": "object",
                        "description": "statistic_id, source, name, unit_of_measurement, "
                        "has_mean, has_sum",
                    },
                    "stats": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Rows: {start, mean|sum|state|min|max, ...}",
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["metadata", "stats"],
            },
            keywords=("import", "statistics", "overwrite", "correct", "backfill", "energy", "fix"),
        ),
        ToolSpec(
            name="stats_adjust_sum",
            summary="Adjust a broken running energy total from a point in time forward "
            "(recorder/adjust_sum_statistics)",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "statistic_id": _STATISTIC_ID,
                    "start_time": {
                        "type": "string",
                        "description": "ISO-8601 UTC start of the adjustment window",
                    },
                    "adjustment": {
                        "type": "number",
                        "description": "Amount to add to the sum (may be negative)",
                    },
                    "adjustment_unit_of_measurement": {
                        "type": ["string", "null"],
                        "default": None,
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["statistic_id", "start_time", "adjustment"],
            },
            keywords=("adjust", "sum", "energy", "total", "kwh", "spike", "fix", "correct"),
        ),
        ToolSpec(
            name="stats_change_unit",
            summary="Change the unit of a statistic and rescale history "
            "(recorder/change_statistics_unit)",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "statistic_id": _STATISTIC_ID,
                    "new_unit_of_measurement": {"type": "string"},
                    "old_unit_of_measurement": {"type": ["string", "null"], "default": None},
                    "dry_run": _DRY_RUN,
                },
                "required": ["statistic_id", "new_unit_of_measurement"],
            },
            keywords=("unit", "convert", "rescale", "wh", "kwh", "change", "statistics"),
        ),
        ToolSpec(
            name="stats_clear",
            summary="Permanently delete long-term statistics for the given ids "
            "(recorder/clear_statistics)",
            tier=Tier.T3_DESTRUCTIVE,
            schema={
                "type": "object",
                "properties": {
                    "statistic_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "statistic_ids to wipe",
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["statistic_ids"],
            },
            keywords=("clear", "delete", "wipe", "remove", "statistics", "purge", "destructive"),
        ),
    ),
)
