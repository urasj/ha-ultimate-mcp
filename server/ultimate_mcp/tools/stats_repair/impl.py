"""stats_repair/ surface implementation — lazy-imported on first call (W5).

Reads and writes to long-term statistics go through the recorder WebSocket
commands on ctx.ha_ws; the one exception is stats_anomaly_scan, which reads the
recorder SQLite DB directly (ctx.db) because there is no WS "give me the raw
rows and flag the weird ones" command.

WS command names verified against the 2026.7 recorder websocket_api module, but
the command set is not exhaustively documented — every ctx.ha_ws.call is wrapped
and degraded to {"error": ..., "note": _WS_NOTE} rather than raised.

Statistics model (recorder):
  * mean-type statistics carry mean/min/max (e.g. temperature).
  * sum-type statistics carry a monotonic `sum` running total plus `state`
    (e.g. an energy meter). A sum that DECREASES between hours is either a meter
    reset (legitimate, usually paired with last_reset) or corruption — we flag it.
  * negative mean/sum on a physical meter is almost always corruption.
"""

from __future__ import annotations

import time
from typing import Any

from ultimate_mcp.context import Context

_WS_NOTE = "verify recorder/* WS command against HA 2026.7"


async def _ws(ctx: Context, command: str, **kwargs: Any) -> Any:
    """Call a recorder WS command, degrading instead of raising."""
    try:
        return await ctx.ha_ws.call(command, **kwargs)
    except Exception as exc:  # noqa: BLE001 — ws may be down / command renamed
        return {"error": str(exc), "note": _WS_NOTE}


def _is_err(value: Any) -> bool:
    return isinstance(value, dict) and "error" in value


def _table_columns(ctx: Context, table: str) -> set[str]:
    try:
        rows = ctx.db.query(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}
    except Exception:  # noqa: BLE001
        return set()


# ------------------------------------------------------------------ T0 reads
async def stats_list(ctx: Context, statistic_type: str | None = None, **_: Any) -> Any:
    kwargs: dict[str, Any] = {}
    if statistic_type:
        kwargs["statistic_type"] = statistic_type
    res = await _ws(ctx, "recorder/list_statistic_ids", **kwargs)
    if _is_err(res):
        return res
    ids = res if isinstance(res, list) else res
    return {
        "statistic_ids": ids,
        "count": len(ids) if isinstance(ids, list) else None,
        "statistic_type": statistic_type,
    }


async def stats_metadata(
    ctx: Context, statistic_ids: list[str] | None = None, **_: Any
) -> Any:
    kwargs: dict[str, Any] = {}
    if statistic_ids:
        kwargs["statistic_ids"] = statistic_ids
    res = await _ws(ctx, "recorder/get_statistics_metadata", **kwargs)
    if _is_err(res):
        return res
    return {"metadata": res, "count": len(res) if isinstance(res, list) else None}


async def stats_info(ctx: Context, **_: Any) -> Any:
    res = await _ws(ctx, "recorder/info")
    if _is_err(res):
        return res
    return {"info": res}


async def stats_anomaly_scan(
    ctx: Context,
    statistic_id: str | None = None,
    days: int = 30,
    spike_factor: float = 10.0,
    **_: Any,
) -> Any:
    """Flag negatives, non-monotonic sums, mean spikes, and missing units.

    Reads the recorder DB directly; window functions require SQLite >= 3.25,
    which every HAOS 2023+ image ships.
    """
    cols = _table_columns(ctx, "statistics")
    if not cols:
        return {"error": "statistics table not found in recorder database"}
    if "start_ts" not in cols:
        return {"error": "statistics.start_ts missing: recorder schema too old (< HA 2023.2)"}
    cutoff = time.time() - days * 86400
    try:
        rows = ctx.db.query(
            """
            SELECT statistic_id, start_ts, mean, sum,
                   LAG(mean) OVER w AS prev_mean,
                   LAG(sum)  OVER w AS prev_sum
            FROM (
                SELECT sm.statistic_id AS statistic_id, sm.id AS mid,
                       s.start_ts AS start_ts, s.mean AS mean, s.sum AS sum,
                       s.metadata_id AS metadata_id
                FROM statistics s JOIN statistics_meta sm ON s.metadata_id = sm.id
                WHERE s.start_ts >= ? AND (? IS NULL OR sm.statistic_id = ?)
            )
            WINDOW w AS (PARTITION BY metadata_id ORDER BY start_ts)
            ORDER BY start_ts
            """,
            params=(cutoff, statistic_id, statistic_id),
            limit=2000,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    anomalies: list[dict[str, Any]] = []
    for r in rows:
        sid = r["statistic_id"]
        ts = r["start_ts"]
        mean, sm = r.get("mean"), r.get("sum")
        prev_mean, prev_sum = r.get("prev_mean"), r.get("prev_sum")
        if mean is not None and mean < 0:
            anomalies.append({"statistic_id": sid, "start_ts": ts, "kind": "negative_mean",
                              "value": mean})
        if sm is not None and sm < 0:
            anomalies.append({"statistic_id": sid, "start_ts": ts, "kind": "negative_sum",
                              "value": sm})
        if prev_sum is not None and sm is not None and sm < prev_sum:
            anomalies.append({"statistic_id": sid, "start_ts": ts, "kind": "sum_decrease",
                              "value": sm, "prev_value": prev_sum,
                              "note": "sum went backwards (meter reset or corruption)"})
        if (
            prev_mean is not None
            and mean is not None
            and abs(prev_mean) > 0
            and abs(mean) > spike_factor * abs(prev_mean)
        ):
            anomalies.append({"statistic_id": sid, "start_ts": ts, "kind": "mean_spike",
                              "value": mean, "prev_value": prev_mean})

    # Metadata-level unit issues (missing unit on a numeric statistic).
    unit_issues: list[dict[str, Any]] = []
    if _table_columns(ctx, "statistics_meta"):
        try:
            metas = ctx.db.query(
                """
                SELECT statistic_id, unit_of_measurement, has_sum
                FROM statistics_meta
                WHERE (? IS NULL OR statistic_id = ?)
                """,
                params=(statistic_id, statistic_id),
                limit=2000,
            )
            for m in metas:
                unit = m.get("unit_of_measurement")
                if unit is None or unit == "":
                    unit_issues.append({"statistic_id": m["statistic_id"],
                                        "kind": "missing_unit", "has_sum": m.get("has_sum")})
        except Exception:  # noqa: BLE001
            pass

    kinds: dict[str, int] = {}
    for a in anomalies:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    return {
        "days": days,
        "cutoff_ts": cutoff,
        "statistic_id": statistic_id,
        "spike_factor": spike_factor,
        "rows_scanned": len(rows),
        "anomaly_count": len(anomalies),
        "counts_by_kind": kinds,
        "anomalies": anomalies,
        "unit_issues": unit_issues,
    }


# ------------------------------------------------------------- T2/T3 writes
async def stats_import(
    ctx: Context, metadata: dict, stats: list, dry_run: bool = True, **_: Any
) -> dict[str, Any]:
    """recorder/import_statistics — import/overwrite corrected statistics rows."""
    payload = {"metadata": metadata, "stats": stats}
    plan = {
        "command": "recorder/import_statistics",
        "payload": payload,
        "rows": len(stats) if isinstance(stats, list) else None,
        "statistic_id": metadata.get("statistic_id") if isinstance(metadata, dict) else None,
    }
    if dry_run:
        plan["dry_run"] = True
        plan["note"] = ("dry run only — no WS call. Re-run with dry_run=false to POST "
                        "recorder/import_statistics (requires a session checkpoint). "
                        + _WS_NOTE)
        return plan
    res = await _ws(ctx, "recorder/import_statistics", **payload)
    plan["dry_run"] = False
    plan["executed"] = not _is_err(res)
    plan["result"] = res
    return plan


async def stats_adjust_sum(
    ctx: Context,
    statistic_id: str,
    start_time: str,
    adjustment: float,
    adjustment_unit_of_measurement: str | None = None,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """recorder/adjust_sum_statistics — fix a broken running energy total."""
    payload: dict[str, Any] = {
        "statistic_id": statistic_id,
        "start_time": start_time,
        "adjustment": adjustment,
    }
    if adjustment_unit_of_measurement is not None:
        payload["adjustment_unit_of_measurement"] = adjustment_unit_of_measurement
    plan = {"command": "recorder/adjust_sum_statistics", "payload": payload}
    if dry_run:
        plan["dry_run"] = True
        plan["note"] = ("dry run only — no WS call. Re-run with dry_run=false to adjust the sum "
                        "(requires a session checkpoint). " + _WS_NOTE)
        return plan
    res = await _ws(ctx, "recorder/adjust_sum_statistics", **payload)
    plan["dry_run"] = False
    plan["executed"] = not _is_err(res)
    plan["result"] = res
    return plan


async def stats_change_unit(
    ctx: Context,
    statistic_id: str,
    new_unit_of_measurement: str,
    old_unit_of_measurement: str | None = None,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """recorder/change_statistics_unit — change unit and rescale history."""
    payload: dict[str, Any] = {
        "statistic_id": statistic_id,
        "new_unit_of_measurement": new_unit_of_measurement,
    }
    if old_unit_of_measurement is not None:
        payload["old_unit_of_measurement"] = old_unit_of_measurement
    plan = {"command": "recorder/change_statistics_unit", "payload": payload}
    if dry_run:
        plan["dry_run"] = True
        plan["note"] = ("dry run only — no WS call. Re-run with dry_run=false to change the unit "
                        "(requires a session checkpoint). " + _WS_NOTE)
        return plan
    res = await _ws(ctx, "recorder/change_statistics_unit", **payload)
    plan["dry_run"] = False
    plan["executed"] = not _is_err(res)
    plan["result"] = res
    return plan


async def stats_clear(
    ctx: Context, statistic_ids: list[str], dry_run: bool = True, **_: Any
) -> dict[str, Any]:
    """recorder/clear_statistics — T3: permanently delete statistics for ids."""
    payload = {"statistic_ids": statistic_ids}
    plan = {
        "command": "recorder/clear_statistics",
        "payload": payload,
        "statistic_ids": statistic_ids,
    }
    if dry_run:
        plan["dry_run"] = True
        plan["note"] = ("dry run only — no WS call. This is DESTRUCTIVE and irreversible; "
                        "re-run with dry_run=false plus destructive_enabled + confirm_token. "
                        + _WS_NOTE)
        return plan
    res = await _ws(ctx, "recorder/clear_statistics", **payload)
    plan["dry_run"] = False
    plan["executed"] = not _is_err(res)
    plan["result"] = res
    return plan
