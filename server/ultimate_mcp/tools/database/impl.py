"""database/ surface implementation — lazy-imported on first call (W3 reference)."""

from __future__ import annotations

import re
from typing import Any

from ultimate_mcp.context import Context

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|pragma|vacuum|reindex)\b", re.I
)


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
