"""realtime/ surface implementation — lazy-imported on first call (W4).

Built on ctx.ha_ws (the HaWsClient) for bus/state tools, ctx.supervisor for log
tailing, and ctx.db for the recorder flatline scan. Every blocking loop is
timeboxed with asyncio.wait_for so a tool always returns a JSON payload.

WS guard: ctx.ha_ws may not be connected (no broker, bad token, sandbox). We
treat NotImplementedError / ConnectionError / HaWsError as "ws unavailable" and
degrade to {"error": "ws unavailable"} rather than raising.

API assumptions flagged for review:
  * automation_triggered event carries data.entity_id (HA core fires this on
    every automation run; confirmed against 2024+ cores).
  * Automation entities expose their numeric config id via state attribute "id";
    trace/get needs domain="automation" + item_id=<that id> + run_id.
  * trace/list returns a list of runs each with a "run_id"; the newest is last.
  * Supervisor log endpoints (/core/logs, /supervisor/logs, /addons/<slug>/logs)
    return plain text, not JSON — see _read_log for the text-vs-json handling.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ultimate_mcp.context import Context

# Exceptions that mean "the WS link is not usable"; caught and degraded.
_WS_DOWN = (NotImplementedError, ConnectionError)


def _ws_error(exc: Exception) -> dict[str, Any]:
    return {"error": "ws unavailable", "detail": str(exc)}


# --------------------------------------------------------------- state / bus


async def wait_for_state(
    ctx: Context, entity_id: str, to_state: str, timeout: float = 30, **_: Any
) -> dict[str, Any]:
    """Block until entity_id reaches to_state (delegates to ha_ws.wait_for_state)."""
    try:
        state = await ctx.ha_ws.wait_for_state(entity_id, to_state, timeout=timeout)
    except asyncio.TimeoutError:
        return {"entity_id": entity_id, "to_state": to_state, "reached": False, "timed_out": True}
    except _WS_DOWN as exc:
        return _ws_error(exc)
    except Exception as exc:  # noqa: BLE001 — HaWsError and friends degrade too
        return _ws_error(exc)
    return {"entity_id": entity_id, "to_state": to_state, "reached": True, "state": state}


async def event_window_capture(
    ctx: Context,
    event_type: str | None = None,
    seconds: float = 5,
    max_events: int = 1000,
    **_: Any,
) -> dict[str, Any]:
    """Subscribe to the bus and collect events for `seconds` (or until max_events)."""
    events: list[dict[str, Any]] = []

    async def _loop() -> None:
        sub = ctx.ha_ws.subscribe(event_type)
        try:
            async for event in sub:
                events.append(event)
                if len(events) >= max_events:
                    return
        finally:
            await sub.aclose()

    try:
        await asyncio.wait_for(_loop(), timeout=seconds)
    except asyncio.TimeoutError:
        pass  # timebox reached — expected exit
    except _WS_DOWN as exc:
        return _ws_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _ws_error(exc)
    return {
        "event_type": event_type,
        "seconds": seconds,
        "count": len(events),
        "truncated": len(events) >= max_events,
        "events": events,
    }


# --------------------------------------------------------------- log follow


def _log_path(source: str) -> str:
    if source == "core":
        return "/core/logs"
    if source == "supervisor":
        return "/supervisor/logs"
    return f"/addons/{source}/logs"


async def _read_log(ctx: Context, path: str) -> str:
    """Fetch a Supervisor log endpoint as text.

    Supervisor log endpoints return plain text, but SupervisorClient.get() calls
    .json(). Prefer the underlying httpx client (._client) for a raw text read;
    fall back to .get() and stringify whatever comes back so test stubs that
    return a string (or dict) still work.
    """
    client = getattr(ctx.supervisor, "_client", None)
    if client is not None:
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.text
    res = await ctx.supervisor.get(path)  # stub path
    return res if isinstance(res, str) else str(res)


async def log_follow(
    ctx: Context,
    source: str = "core",
    seconds: float = 5,
    until_match: str | None = None,
    poll_interval: float = 1,
    tail_lines: int = 200,
    **_: Any,
) -> dict[str, Any]:
    """Poll a log endpoint until `seconds` elapse or `until_match` hits a new line."""
    path = _log_path(source)
    pattern = re.compile(until_match) if until_match else None
    state: dict[str, Any] = {"text": "", "seen": 0, "matched": False, "matched_line": None}

    async def _follow() -> None:
        first = True
        while True:
            try:
                text = await _read_log(ctx, path)
            except Exception as exc:  # noqa: BLE001
                state["fetch_error"] = str(exc)
                return
            state["text"] = text
            new = text[state["seen"]:]
            state["seen"] = len(text)
            if pattern is not None and new:
                for line in new.splitlines():
                    if pattern.search(line):
                        state["matched"] = True
                        state["matched_line"] = line.strip()[:400]
                        return
            if first and pattern is None:
                # No stop-pattern: one snapshot is enough, then honour the window.
                first = False
            await asyncio.sleep(poll_interval)

    try:
        await asyncio.wait_for(_follow(), timeout=seconds)
    except asyncio.TimeoutError:
        pass

    if "fetch_error" in state:
        return {"error": f"log fetch failed for {source}: {state['fetch_error']}", "source": source}
    lines = state["text"].splitlines()
    return {
        "source": source,
        "path": path,
        "matched": state["matched"],
        "matched_line": state["matched_line"],
        "total_lines": len(lines),
        "tail": lines[-tail_lines:],
    }


# --------------------------------------------------------------- flatline scan


def _has_states_tables(ctx: Context) -> bool:
    try:
        rows = ctx.db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('states','states_meta')"
        )
    except Exception:  # noqa: BLE001
        return False
    return {r["name"] for r in rows} >= {"states", "states_meta"}


async def state_flatline_scan(
    ctx: Context, threshold_hours: float = 24, top: int = 100, **_: Any
) -> dict[str, Any]:
    """Entities whose most recent recorder state row is older than the threshold."""
    if not _has_states_tables(ctx):
        return {"error": "states/states_meta not found in recorder database"}
    now = time.time()
    cutoff = now - threshold_hours * 3600
    try:
        rows = ctx.db.query(
            """
            SELECT sm.entity_id AS entity_id, MAX(s.last_updated_ts) AS last_ts
            FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id
            GROUP BY sm.entity_id
            HAVING MAX(s.last_updated_ts) < ?
            ORDER BY last_ts ASC
            LIMIT ?
            """,
            params=(cutoff, top),
            limit=top,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    for r in rows:
        last = r.get("last_ts")
        r["stale_hours"] = round((now - last) / 3600, 1) if last is not None else None
    return {
        "threshold_hours": threshold_hours,
        "cutoff_ts": cutoff,
        "flatlined_count": len(rows),
        "flatlined": rows,
    }


# --------------------------------------------------------------- trace next run


async def _automation_item_id(ctx: Context, entity_id: str) -> str | None:
    """Resolve an automation entity_id to its numeric trace item_id (attribute 'id')."""
    try:
        states = await ctx.ha_ws.call("get_states")
    except Exception:  # noqa: BLE001
        return None
    for st in states or []:
        if st.get("entity_id") == entity_id:
            attrs = st.get("attributes") or {}
            return attrs.get("id")
    return None


async def trace_next_run(
    ctx: Context, entity_id: str, timeout: float = 60, **_: Any
) -> dict[str, Any]:
    """Arm on automation_triggered for entity_id, wait for a fire, return the trace."""

    async def _arm() -> dict[str, Any]:
        sub = ctx.ha_ws.subscribe("automation_triggered")
        try:
            async for event in sub:
                data = event.get("data") or {}
                if data.get("entity_id") == entity_id:
                    return event
            raise ConnectionError("event stream ended before automation fired")
        finally:
            await sub.aclose()

    try:
        trigger_event = await asyncio.wait_for(_arm(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"entity_id": entity_id, "fired": False, "timed_out": True}
    except _WS_DOWN as exc:
        return _ws_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _ws_error(exc)

    out: dict[str, Any] = {"entity_id": entity_id, "fired": True, "trigger_event": trigger_event}
    item_id = await _automation_item_id(ctx, entity_id)
    out["item_id"] = item_id
    if item_id is None:
        out["trace"] = None
        out["note"] = "fired but could not resolve automation item_id (attribute 'id') for trace/get"
        return out
    try:
        runs = await ctx.ha_ws.call("trace/list", domain="automation", item_id=item_id)
        run_id = runs[-1].get("run_id") if runs else None
        out["run_id"] = run_id
        if run_id is not None:
            out["trace"] = await ctx.ha_ws.call(
                "trace/get", domain="automation", item_id=item_id, run_id=run_id
            )
        else:
            out["trace"] = None
    except Exception as exc:  # noqa: BLE001
        out["trace"] = None
        out["trace_error"] = str(exc)
    return out
