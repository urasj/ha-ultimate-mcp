"""zigbee/ surface implementation — ZHA deep ops via the core WS API (W4b).

Every ZHA WS command is wrapped in try/except and degrades to
    {"error": <str>, "note": "verify zha/* WS command name for 2026.7", ...}
instead of raising, because the 2026.7 ZHA WS command spellings are NOT
exhaustively documented. Where a spelling is uncertain it is flagged with a
# VERIFY comment and the best-known name from core source / the community gist
is used.

Command names used (cross-referenced against homeassistant.components.zha
websocket_api and the zigpy/ZHA source; confirm against the live 2026.7 box):
  zha/devices                                  -> list devices
  zha/device                                   -> one device (ieee=...)
  zha/devices/clusters/attributes/value        -> read attribute value
  zha/devices/clusters/attributes/write        -> write attribute value   # VERIFY
  zha/devices/bindings                         -> list bindings (ieee=...) # VERIFY
  zha/devices/bindings/bind                    -> create binding           # VERIFY
  zha/devices/bindings/unbind                  -> remove binding           # VERIFY
  zha/devices/reconfigure                      -> reconfigure a device
  zha/network/settings                         -> network settings
  zha/network/backup                           -> coordinator NVM backup
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ultimate_mcp.context import Context

_NOTE = "verify zha/* WS command name for 2026.7"


async def _safe_ws(ctx: Context, command: str, **kwargs: Any) -> tuple[Any, dict | None]:
    """Call a ZHA WS command; return (result, None) or (None, error_payload).

    All exceptions (HaWsError for unknown/bad command, ConnectionError, etc.)
    degrade to a flagged error dict so the surface never raises at the tool
    boundary.
    """
    try:
        return await ctx.ha_ws.call(command, **kwargs), None
    except Exception as exc:  # noqa: BLE001 — degrade, never raise from a tool
        return None, {"error": str(exc), "note": _NOTE, "command": command}


# ------------------------------------------------------------------ T0 reads
async def zha_devices(ctx: Context, **_: Any) -> Any:
    """List all ZHA devices with the fields most useful for a mesh audit."""
    result, err = await _safe_ws(ctx, "zha/devices")
    if err is not None:
        return err
    devices = result if isinstance(result, list) else (result or {}).get("devices", [])
    slim = [
        {
            "ieee": d.get("ieee"),
            "nwk": d.get("nwk"),
            "name": d.get("user_given_name") or d.get("name"),
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
            "lqi": d.get("lqi"),
            "rssi": d.get("rssi"),
            "last_seen": d.get("last_seen"),
            "available": d.get("available"),
            "power_source": d.get("power_source"),
            "device_type": d.get("device_type"),  # Coordinator/Router/EndDevice
        }
        for d in (devices or [])
    ]
    return {"count": len(slim), "devices": slim}


async def zha_device_detail(ctx: Context, ieee: str, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "zha/device", ieee=ieee)
    if err is not None:
        return err
    return result


async def zha_cluster_read(
    ctx: Context,
    ieee: str,
    endpoint_id: int,
    cluster_id: int,
    attribute: int | str,
    cluster_type: str = "in",
    manufacturer: int | None = None,
    **_: Any,
) -> Any:
    kwargs: dict[str, Any] = {
        "ieee": ieee,
        "endpoint_id": endpoint_id,
        "cluster_id": cluster_id,
        "cluster_type": cluster_type,
        "attribute": attribute,
    }
    if manufacturer is not None:
        kwargs["manufacturer"] = manufacturer
    result, err = await _safe_ws(ctx, "zha/devices/clusters/attributes/value", **kwargs)
    if err is not None:
        return err
    return {
        "ieee": ieee,
        "endpoint_id": endpoint_id,
        "cluster_id": cluster_id,
        "cluster_type": cluster_type,
        "attribute": attribute,
        "value": result,
    }


async def zha_bindings_list(ctx: Context, ieee: str, **_: Any) -> Any:
    # VERIFY: some releases surface bindings only inside zha/device detail
    # (device["bindings"]) rather than a dedicated command.
    result, err = await _safe_ws(ctx, "zha/devices/bindings", ieee=ieee)
    if err is not None:
        # Fall back to the detail payload's bindings, if present.
        detail, derr = await _safe_ws(ctx, "zha/device", ieee=ieee)
        if derr is None and isinstance(detail, dict) and detail.get("bindings") is not None:
            return {"ieee": ieee, "bindings": detail["bindings"], "source": "zha/device"}
        return err
    return {"ieee": ieee, "bindings": result}


async def zha_network_settings(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "zha/network/settings")
    if err is not None:
        return err
    return result


# ------------------------------------------------------ topology (T0, hybrid)
def _read_zigbee_db_topology(ctx: Context) -> dict[str, Any] | None:
    """Parse neighbor/route tables straight from zigbee.db if reachable.

    zigpy's application DB lives at <config>/zigbee.db. Table names are schema-
    versioned (e.g. neighbors_v12, routes_v12), so we discover them at runtime
    rather than hardcoding a revision. Returns None if the DB is unreachable so
    the caller can fall back to the WS-derived topology.
    """
    try:
        path = ctx.fs.resolve("zigbee.db")
    except Exception:  # noqa: BLE001 — path escapes root / fs unavailable
        return None
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except Exception:  # noqa: BLE001
        return None
    try:
        con.row_factory = sqlite3.Row
        tables = {
            r["name"]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        def _latest(prefix: str) -> str | None:
            matches = sorted(t for t in tables if t == prefix or t.startswith(prefix + "_v"))
            return matches[-1] if matches else None

        neighbors_tbl = _latest("neighbors")
        routes_tbl = _latest("routes")
        edges: list[dict[str, Any]] = []
        if neighbors_tbl:
            for row in con.execute(f"SELECT * FROM {neighbors_tbl}"):  # noqa: S608 — name from schema
                r = dict(row)
                edges.append(
                    {
                        "source": r.get("device_ieee") or r.get("ieee"),
                        "neighbor": r.get("ieee") or r.get("extended_pan_id"),
                        "lqi": r.get("lqi"),
                        "relationship": r.get("relationship"),
                        "depth": r.get("depth"),
                    }
                )
        routes: list[dict[str, Any]] = []
        if routes_tbl:
            for row in con.execute(f"SELECT * FROM {routes_tbl}"):  # noqa: S608
                routes.append(dict(row))
        return {
            "source": f"zigbee.db:{neighbors_tbl or '?'}",
            "edges": edges,
            "routes": routes,
        }
    except Exception:  # noqa: BLE001 — unknown table shape; fall back to WS
        return None
    finally:
        con.close()


async def zha_topology_graph(ctx: Context, **_: Any) -> Any:
    """Build a neighbor / link-quality map.

    Prefers parsing zigbee.db (neighbor + route tables) when the config mount is
    reachable; otherwise derives edges from each device's `neighbors` list in the
    ZHA WS device inventory.
    """
    db_topo = _read_zigbee_db_topology(ctx)
    if db_topo is not None and db_topo.get("edges"):
        return db_topo

    result, err = await _safe_ws(ctx, "zha/devices")
    if err is not None:
        # No DB and no WS — surface both signals.
        return {**err, "note": f"{_NOTE}; zigbee.db also unreachable"}
    devices = result if isinstance(result, list) else (result or {}).get("devices", [])

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for d in devices or []:
        ieee = d.get("ieee")
        nodes.append(
            {
                "ieee": ieee,
                "nwk": d.get("nwk"),
                "name": d.get("user_given_name") or d.get("name"),
                "device_type": d.get("device_type"),
                "lqi": d.get("lqi"),
                "rssi": d.get("rssi"),
                "available": d.get("available"),
            }
        )
        for n in d.get("neighbors") or []:
            edges.append(
                {
                    "source": ieee,
                    "neighbor": n.get("ieee"),
                    "lqi": n.get("lqi"),
                    "relationship": n.get("relationship"),
                    "depth": n.get("depth"),
                }
            )
    return {
        "source": "zha/devices:neighbors",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


# --------------------------------------------------------- T1 reversible
async def zha_cluster_write(
    ctx: Context,
    ieee: str,
    endpoint_id: int,
    cluster_id: int,
    attribute: int | str,
    value: Any,
    cluster_type: str = "in",
    manufacturer: int | None = None,
    dry_run: bool = True,
    **_: Any,
) -> Any:
    plan = {
        "command": "zha/devices/clusters/attributes/write",  # VERIFY spelling for 2026.7
        "ieee": ieee,
        "endpoint_id": endpoint_id,
        "cluster_id": cluster_id,
        "cluster_type": cluster_type,
        "attribute": attribute,
        "value": value,
        "manufacturer": manufacturer,
    }
    if dry_run:
        return {
            "dry_run": True,
            "intended_write": plan,
            "note": "dry run only — no attribute written. Re-run with dry_run=false to write. "
            + _NOTE,
        }
    kwargs: dict[str, Any] = {
        "ieee": ieee,
        "endpoint_id": endpoint_id,
        "cluster_id": cluster_id,
        "cluster_type": cluster_type,
        "attribute": attribute,
        "value": value,
    }
    if manufacturer is not None:
        kwargs["manufacturer"] = manufacturer
    result, err = await _safe_ws(ctx, "zha/devices/clusters/attributes/write", **kwargs)
    if err is not None:
        return err
    return {"dry_run": False, "written": plan, "result": result}


async def zha_reconfigure(ctx: Context, ieee: str, dry_run: bool = True, **_: Any) -> Any:
    if dry_run:
        return {
            "dry_run": True,
            "command": "zha/devices/reconfigure",
            "ieee": ieee,
            "note": "dry run only — device not reconfigured. Re-run with dry_run=false. " + _NOTE,
        }
    result, err = await _safe_ws(ctx, "zha/devices/reconfigure", ieee=ieee)
    if err is not None:
        return err
    return {"dry_run": False, "ieee": ieee, "result": result}


async def _bind_impl(
    ctx: Context, command: str, source_ieee: str, target_ieee: str, dry_run: bool
) -> Any:
    if dry_run:
        return {
            "dry_run": True,
            "command": command,
            "source_ieee": source_ieee,
            "target_ieee": target_ieee,
            "note": "dry run only — binding not changed. Re-run with dry_run=false. " + _NOTE,
        }
    result, err = await _safe_ws(
        ctx, command, source_ieee=source_ieee, target_ieee=target_ieee
    )
    if err is not None:
        return err
    return {
        "dry_run": False,
        "source_ieee": source_ieee,
        "target_ieee": target_ieee,
        "result": result,
    }


async def zha_bind(
    ctx: Context, source_ieee: str, target_ieee: str, dry_run: bool = True, **_: Any
) -> Any:
    # VERIFY: command may be "zha/devices/bindings/bind" or "zha/devices/bind".
    return await _bind_impl(ctx, "zha/devices/bindings/bind", source_ieee, target_ieee, dry_run)


async def zha_unbind(
    ctx: Context, source_ieee: str, target_ieee: str, dry_run: bool = True, **_: Any
) -> Any:
    return await _bind_impl(ctx, "zha/devices/bindings/unbind", source_ieee, target_ieee, dry_run)


# --------------------------------------------------------------- T2 risky
async def zha_coordinator_backup(ctx: Context, dry_run: bool = True, **_: Any) -> Any:
    """Create a coordinator NVM/network backup (network keys, PAN id, device table).

    Reversible-in-spirit but T2: a backup snapshot is the prerequisite for radio
    migration / disaster recovery, so we gate it behind an explicit dry_run=false.
    """
    if dry_run:
        return {
            "dry_run": True,
            "command": "zha/network/backup",
            "note": "dry run only — will create a coordinator NVM/network backup "
            "(network keys, PAN id, device table) when re-run with dry_run=false. " + _NOTE,
        }
    result, err = await _safe_ws(ctx, "zha/network/backup")
    if err is not None:
        return err
    return {"dry_run": False, "backup": result}
