"""database/ surface implementation — lazy-imported on first call (W3).

Schema-version notes (target: 2026.7-era recorder, schema rev ~50):
- states.metadata_id -> states_meta.entity_id (entity_id column dropped from
  states in schema 38 / HA 2023.4). Pre-2023.4 databases are NOT supported by
  the join-based tools; they degrade to an {"error": ...} payload.
- Timestamps are epoch floats (last_updated_ts / time_fired_ts / start_ts)
  since schema 31 / HA 2023.2. We probe columns via PRAGMA table_info and
  fall back to the legacy DATETIME-string columns where cheap.
- events.event_type_id -> event_types.event_type since schema 37 / HA 2023.4;
  the legacy inline events.event_type column is used as a fallback.
- recorder_runs still uses DATETIME-string start/end columns in 2026.x, so we
  SELECT * rather than hardcoding names.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ultimate_mcp.context import Context

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|pragma|vacuum|reindex)\b", re.I
)


def _table_columns(ctx: Context, table: str) -> set[str]:
    """Cheap runtime schema probe. Empty set means the table is missing."""
    try:
        rows = ctx.db.query(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}
    except Exception:  # noqa: BLE001 — locked/missing db handled by callers
        return set()


async def db_query(ctx: Context, sql: str, limit: int = 200, **_: Any) -> list[dict]:
    if _FORBIDDEN.search(sql):
        raise PermissionError("db_query is read-only; use db_purge/db_execute (T2/T3) for writes")
    return ctx.db.query(sql, limit=min(limit, 2000))


async def db_schema(ctx: Context, **_: Any) -> dict[str, Any]:
    tables = ctx.db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    rev = ctx.db.query("SELECT * FROM schema_changes ORDER BY change_id DESC LIMIT 1")
    return {"tables": [t["name"] for t in tables], "schema_revision": rev[0] if rev else None}


async def db_size_report(ctx: Context, **_: Any) -> dict[str, Any]:
    size = ctx.db.db_path.stat().st_size
    counts = {}
    for table in ("states", "state_attributes", "events", "statistics", "statistics_short_term"):
        try:
            counts[table] = ctx.db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        except Exception as exc:  # noqa: BLE001 — table may not exist in this schema rev
            counts[table] = f"error: {exc}"
    return {"db_bytes": size, "db_mb": round(size / 1_048_576, 1), "row_counts": counts}


async def db_entity_cost(ctx: Context, top: int = 25, **_: Any) -> list[dict]:
    return ctx.db.query(
        """
        SELECT sm.entity_id, COUNT(*) AS state_rows
        FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id
        GROUP BY sm.entity_id ORDER BY state_rows DESC LIMIT ?
        """,
        params=(top,),
        limit=top,
    )


async def db_purge_preview(ctx: Context, keep_days: int = 10, **_: Any) -> dict[str, Any]:
    rows = ctx.db.query(
        """
        SELECT COUNT(*) AS purgeable FROM states
        WHERE last_updated_ts < (strftime('%s','now') - ? * 86400)
        """,
        params=(keep_days,),
    )
    return {"keep_days": keep_days, "purgeable_state_rows": rows[0]["purgeable"]}


async def db_churn_top(ctx: Context, hours: int = 24, top: int = 25, **_: Any) -> Any:
    """Entities with the most state rows written inside the window."""
    cols = _table_columns(ctx, "states")
    if not cols:
        return {"error": "states table not found in recorder database"}
    if "last_updated_ts" not in cols or "metadata_id" not in cols:
        # Pre-2023.4 schema (inline entity_id / DATETIME strings) — out of scope.
        return {"error": "recorder schema too old: needs last_updated_ts + metadata_id (>= HA 2023.4)"}
    cutoff = time.time() - hours * 3600
    try:
        rows = ctx.db.query(
            """
            SELECT sm.entity_id, COUNT(*) AS state_rows
            FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id
            WHERE s.last_updated_ts >= ?
            GROUP BY sm.entity_id ORDER BY state_rows DESC LIMIT ?
            """,
            params=(cutoff, top),
            limit=top,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {"hours": hours, "cutoff_ts": cutoff, "entities": rows}


async def db_event_firehose(ctx: Context, hours: int = 24, top: int = 25, **_: Any) -> Any:
    """Event types by row volume inside the window."""
    cols = _table_columns(ctx, "events")
    if not cols:
        return {"error": "events table not found in recorder database"}
    ts_col = "time_fired_ts" if "time_fired_ts" in cols else None
    cutoff = time.time() - hours * 3600
    try:
        if "event_type_id" in cols and ts_col:
            # Modern schema (>= 37): event_type_ids -> event_types lookup table.
            rows = ctx.db.query(
                f"""
                SELECT et.event_type, COUNT(*) AS events
                FROM events e JOIN event_types et ON e.event_type_id = et.event_type_id
                WHERE e.{ts_col} >= ?
                GROUP BY et.event_type ORDER BY events DESC LIMIT ?
                """,
                params=(cutoff, top),
                limit=top,
            )
        elif "event_type" in cols and ts_col:
            # Legacy inline event_type column (schema < 37).
            rows = ctx.db.query(
                f"""
                SELECT event_type, COUNT(*) AS events FROM events
                WHERE {ts_col} >= ?
                GROUP BY event_type ORDER BY events DESC LIMIT ?
                """,
                params=(cutoff, top),
                limit=top,
            )
        else:
            return {"error": "events table has neither event_type_id nor event_type/time_fired_ts"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {"hours": hours, "cutoff_ts": cutoff, "event_types": rows}


async def db_stats_gaps(
    ctx: Context, statistic_id: str | None = None, days: int = 7, **_: Any
) -> Any:
    """Missing hourly rows and NULL-value runs in long-term statistics.

    statistics rows are hourly (start_ts spaced 3600 s apart per metadata_id);
    any spacing > 3600 s is a gap. Requires SQLite >= 3.25 (window functions),
    which every HAOS 2023+ image ships.
    """
    cols = _table_columns(ctx, "statistics")
    if not cols:
        return {"error": "statistics table not found in recorder database"}
    if "start_ts" not in cols:
        return {"error": "statistics.start_ts missing: recorder schema too old (< HA 2023.2)"}
    cutoff = time.time() - days * 86400
    try:
        gaps = ctx.db.query(
            """
            SELECT statistic_id,
                   prev_ts + 3600.0 AS gap_start_ts,
                   start_ts        AS gap_end_ts,
                   CAST((start_ts - prev_ts) / 3600 AS INTEGER) - 1 AS missing_hours
            FROM (
                SELECT sm.statistic_id AS statistic_id, s.start_ts AS start_ts,
                       LAG(s.start_ts) OVER (
                           PARTITION BY s.metadata_id ORDER BY s.start_ts
                       ) AS prev_ts
                FROM statistics s JOIN statistics_meta sm ON s.metadata_id = sm.id
                WHERE s.start_ts >= ? AND (? IS NULL OR sm.statistic_id = ?)
            )
            WHERE prev_ts IS NOT NULL AND start_ts - prev_ts > 3600
            ORDER BY missing_hours DESC
            """,
            params=(cutoff, statistic_id, statistic_id),
        )
        null_runs = ctx.db.query(
            """
            SELECT sm.statistic_id, COUNT(*) AS null_rows
            FROM statistics s JOIN statistics_meta sm ON s.metadata_id = sm.id
            WHERE s.start_ts >= ? AND (? IS NULL OR sm.statistic_id = ?)
              AND s.mean IS NULL AND s.sum IS NULL
            GROUP BY sm.statistic_id ORDER BY null_rows DESC
            """,
            params=(cutoff, statistic_id, statistic_id),
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {
        "days": days,
        "cutoff_ts": cutoff,
        "statistic_id": statistic_id,
        "gaps": gaps,
        "null_value_rows": null_runs,
    }


async def db_attr_bloat(ctx: Context, top: int = 25, **_: Any) -> Any:
    """Largest shared attribute payloads with how many state rows reference each."""
    cols = _table_columns(ctx, "state_attributes")
    if not cols:
        return {"error": "state_attributes table not found (recorder schema < HA 2022.4?)"}
    try:
        rows = ctx.db.query(
            """
            SELECT sa.attributes_id,
                   LENGTH(sa.shared_attrs)          AS attr_bytes,
                   COUNT(s.state_id)                AS used_by_states,
                   MIN(sm.entity_id)                AS example_entity,
                   SUBSTR(sa.shared_attrs, 1, 300)  AS preview
            FROM state_attributes sa
            LEFT JOIN states s      ON s.attributes_id = sa.attributes_id
            LEFT JOIN states_meta sm ON s.metadata_id = sm.metadata_id
            GROUP BY sa.attributes_id
            ORDER BY attr_bytes DESC LIMIT ?
            """,
            params=(top,),
            limit=top,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {"top": top, "attributes": rows}


async def db_recorder_advisor(ctx: Context, threshold_rows: int = 10000, **_: Any) -> Any:
    """Combine total entity cost with 24 h churn into recorder exclude candidates."""
    try:
        totals = ctx.db.query(
            """
            SELECT sm.entity_id, COUNT(*) AS state_rows
            FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id
            GROUP BY sm.entity_id HAVING state_rows >= ?
            ORDER BY state_rows DESC
            """,
            params=(threshold_rows,),
            limit=200,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    churn = await db_churn_top(ctx, hours=24, top=200)
    churn_map: dict[str, int] = {}
    if isinstance(churn, dict) and "entities" in churn:
        churn_map = {r["entity_id"]: r["state_rows"] for r in churn["entities"]}
    candidates = [
        {
            "entity_id": t["entity_id"],
            "state_rows": t["state_rows"],
            "rows_last_24h": churn_map.get(t["entity_id"], 0),
        }
        for t in totals
    ]
    projected = sum(c["state_rows"] for c in candidates)
    if candidates:
        yaml_lines = ["recorder:", "  exclude:", "    entities:"]
        yaml_lines += [f"      - {c['entity_id']}" for c in candidates]
        yaml_block = "\n".join(yaml_lines) + "\n"
    else:
        yaml_block = f"# no entities exceed threshold_rows={threshold_rows}; nothing to exclude\n"
    return {
        "threshold_rows": threshold_rows,
        "candidates": candidates,
        "projected_row_savings": projected,
        "recorder_yaml": yaml_block,
        "note": "paste recorder_yaml into configuration.yaml, then run db_purge_execute "
        "with entity_ids to reclaim existing rows",
    }


async def db_integrity_check(ctx: Context, **_: Any) -> dict[str, Any]:
    """PRAGMA integrity_check plus freelist/page statistics (all read-safe pragmas)."""
    out: dict[str, Any] = {}
    try:
        check = ctx.db.query("PRAGMA integrity_check")
        out["integrity_check"] = [r.get("integrity_check") for r in check]
        out["ok"] = out["integrity_check"] == ["ok"]
    except Exception as exc:  # noqa: BLE001
        out["integrity_check"] = None
        out["ok"] = False
        out["error"] = str(exc)
        return out
    for pragma in ("freelist_count", "page_count", "page_size"):
        try:
            out[pragma] = ctx.db.query(f"PRAGMA {pragma}")[0][pragma]
        except Exception as exc:  # noqa: BLE001
            out[pragma] = f"error: {exc}"
    if isinstance(out.get("page_size"), int):
        if isinstance(out.get("page_count"), int):
            out["db_bytes"] = out["page_count"] * out["page_size"]
        if isinstance(out.get("freelist_count"), int):
            out["freelist_bytes"] = out["freelist_count"] * out["page_size"]
            out["reclaimable_pct"] = (
                round(100 * out["freelist_count"] / out["page_count"], 1)
                if isinstance(out.get("page_count"), int) and out["page_count"]
                else 0.0
            )
    return out


async def db_restart_history(ctx: Context, limit: int = 20, **_: Any) -> Any:
    """recorder_runs rows, newest first. SELECT * on purpose: recorder_runs still
    carries legacy DATETIME-string start/end columns in 2026.x and we do not want
    to hardcode that shape."""
    if not _table_columns(ctx, "recorder_runs"):
        return {"error": "recorder_runs table not found in recorder database"}
    try:
        rows = ctx.db.query(
            "SELECT * FROM recorder_runs ORDER BY run_id DESC LIMIT ?",
            params=(limit,),
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {"runs": rows, "unclean_shutdowns": sum(1 for r in rows if r.get("closed_incorrectly"))}


async def db_purge_execute(
    ctx: Context,
    keep_days: int,
    repack: bool = False,
    entity_ids: list[str] | None = None,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """T2: purge via the recorder.purge / recorder.purge_entities HA services.

    Never issues raw DELETEs — the recorder service owns WAL/locking semantics.
    Safety kernel enforces the checkpoint gate before dry_run=False reaches here.
    """
    service = "purge_entities" if entity_ids else "purge"
    body: dict[str, Any] = {"keep_days": keep_days}
    if entity_ids:
        body["entity_id"] = entity_ids
    else:
        body["repack"] = repack

    # Build the preview plan (works for both dry and wet runs).
    cutoff_expr = "(strftime('%s','now') - ? * 86400)"
    plan: dict[str, Any] = {
        "service": f"recorder.{service}",
        "service_data": body,
        "keep_days": keep_days,
        "repack": repack,
    }
    try:
        if entity_ids:
            marks = ",".join("?" for _e in entity_ids)
            rows = ctx.db.query(
                f"""
                SELECT sm.entity_id, COUNT(*) AS purgeable_rows
                FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                WHERE sm.entity_id IN ({marks}) AND s.last_updated_ts < {cutoff_expr}
                GROUP BY sm.entity_id
                """,
                params=(*entity_ids, keep_days),
            )
            plan["per_entity"] = rows
            plan["purgeable_state_rows"] = sum(r["purgeable_rows"] for r in rows)
        else:
            rows = ctx.db.query(
                f"SELECT COUNT(*) AS purgeable FROM states WHERE last_updated_ts < {cutoff_expr}",
                params=(keep_days,),
            )
            plan["purgeable_state_rows"] = rows[0]["purgeable"]
    except Exception as exc:  # noqa: BLE001
        plan["preview_error"] = str(exc)

    if dry_run:
        plan["dry_run"] = True
        plan["note"] = (
            f"dry run only — no service called. Re-run with dry_run=false to POST "
            f"services/recorder/{service} via the core API (requires a session checkpoint)."
        )
        return plan

    result = await ctx.supervisor.core_api("POST", f"services/recorder/{service}", body)
    plan["dry_run"] = False
    plan["executed"] = True
    plan["service_result"] = result
    return plan
