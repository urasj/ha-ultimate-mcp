"""registries/ surface implementation — lazy-imported on first call (W5).

Everything goes through the core WebSocket config/*_registry/* commands via
ctx.ha_ws.call(). Each call is wrapped so that any failure (HaWsError for an
unknown command name, ConnectionError, etc.) degrades to

    {"error": <str>, "note": "verify WS command for 2026.7", "command": <cmd>}

instead of raising at the tool boundary. Mutating tools default to dry_run=True
and, when previewing, return the exact WS command + payload they would send.

WS command spellings (cross-referenced against homeassistant.components.config
websocket_api handlers; confirm against the live 2026.7 box):
  config/entity_registry/list                  -> list entities
  config/entity_registry/list_for_display      -> compact list (not used; full list preferred)
  config/entity_registry/get                   -> one entity (entity_id=...)
  config/entity_registry/update                -> update entity (entity_id=..., **changes)
  config/entity_registry/remove                -> remove entity (entity_id=...)
  config/device_registry/list                  -> list devices
  config/device_registry/update                -> update device (device_id=..., **changes)
  config/device_registry/remove_config_entry   -> detach device from a config entry   # VERIFY
  config/area_registry/list|create|update|delete
  config/floor_registry/list
  config/label_registry/list|create
  config/category_registry/list                -> categories (scope=...)               # VERIFY scope arg
  homeassistant/expose_entity                  -> set Assist exposure                  # VERIFY
"""

from __future__ import annotations

from typing import Any

from ultimate_mcp.context import Context

_NOTE = "verify WS command for 2026.7"


async def _safe_ws(ctx: Context, command: str, **kwargs: Any) -> tuple[Any, dict | None]:
    """Call a registry WS command; return (result, None) or (None, error_payload)."""
    try:
        return await ctx.ha_ws.call(command, **kwargs), None
    except Exception as exc:  # noqa: BLE001 — degrade, never raise from a tool
        return None, {"error": str(exc), "note": _NOTE, "command": command}


def _as_list(result: Any, *keys: str) -> list[dict[str, Any]]:
    """Normalise a WS result to a list of rows.

    Registry list handlers return a bare list, but some HA versions wrap it in
    {"<key>": [...]}. Accept both shapes so the tool is robust to the wrapping.
    """
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for k in keys:
            if isinstance(result.get(k), list):
                return result[k]
    return []


# ------------------------------------------------------------------ T0 reads
async def entity_list(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "config/entity_registry/list")
    if err is not None:
        return err
    rows = _as_list(result, "entities")
    slim = [
        {
            "entity_id": e.get("entity_id"),
            "name": e.get("name") or e.get("original_name"),
            "platform": e.get("platform"),
            "device_id": e.get("device_id"),
            "area_id": e.get("area_id"),
            "labels": e.get("labels", []),
            "categories": e.get("categories", {}),
            "hidden_by": e.get("hidden_by"),
            "disabled_by": e.get("disabled_by"),
            "entity_category": e.get("entity_category"),
        }
        for e in rows
    ]
    return {"count": len(slim), "entities": slim}


async def entity_get(ctx: Context, entity_id: str, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "config/entity_registry/get", entity_id=entity_id)
    if err is not None:
        return err
    return {"entity_id": entity_id, "entity": result}


async def device_list(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "config/device_registry/list")
    if err is not None:
        return err
    rows = _as_list(result, "devices")
    slim = [
        {
            "id": d.get("id"),
            "name": d.get("name_by_user") or d.get("name"),
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
            "area_id": d.get("area_id"),
            "labels": d.get("labels", []),
            "config_entries": d.get("config_entries", []),
            "disabled_by": d.get("disabled_by"),
        }
        for d in rows
    ]
    return {"count": len(slim), "devices": slim}


async def area_list(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "config/area_registry/list")
    if err is not None:
        return err
    rows = _as_list(result, "areas")
    return {"count": len(rows), "areas": rows}


async def floor_list(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "config/floor_registry/list")
    if err is not None:
        return err
    rows = _as_list(result, "floors")
    return {"count": len(rows), "floors": rows}


async def label_list(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "config/label_registry/list")
    if err is not None:
        return err
    rows = _as_list(result, "labels")
    return {"count": len(rows), "labels": rows}


async def category_list(ctx: Context, scope: str = "automation", **_: Any) -> Any:
    # VERIFY: category registry list requires a "scope" argument in 2026.x.
    result, err = await _safe_ws(ctx, "config/category_registry/list", scope=scope)
    if err is not None:
        return err
    rows = _as_list(result, "categories")
    return {"scope": scope, "count": len(rows), "categories": rows}


# ----------------------------------------------------------- T1 reversible
async def entity_update(
    ctx: Context, entity_id: str, updates: dict[str, Any], dry_run: bool = True, **_: Any
) -> Any:
    payload = {"entity_id": entity_id, **updates}
    if dry_run:
        return {
            "dry_run": True,
            "command": "config/entity_registry/update",
            "payload": payload,
            "note": "re-run with dry_run=false to apply this update",
        }
    result, err = await _safe_ws(ctx, "config/entity_registry/update", **payload)
    if err is not None:
        return err
    return {"dry_run": False, "updated": True, "entity_id": entity_id, "result": result}


async def device_update(
    ctx: Context, device_id: str, updates: dict[str, Any], dry_run: bool = True, **_: Any
) -> Any:
    payload = {"device_id": device_id, **updates}
    if dry_run:
        return {
            "dry_run": True,
            "command": "config/device_registry/update",
            "payload": payload,
            "note": "re-run with dry_run=false to apply this update",
        }
    result, err = await _safe_ws(ctx, "config/device_registry/update", **payload)
    if err is not None:
        return err
    return {"dry_run": False, "updated": True, "device_id": device_id, "result": result}


async def area_create(
    ctx: Context,
    name: str,
    floor_id: str | None = None,
    labels: list[str] | None = None,
    icon: str | None = None,
    dry_run: bool = True,
    **_: Any,
) -> Any:
    payload: dict[str, Any] = {"name": name}
    if floor_id is not None:
        payload["floor_id"] = floor_id
    if labels:
        payload["labels"] = labels
    if icon is not None:
        payload["icon"] = icon
    if dry_run:
        return {
            "dry_run": True,
            "command": "config/area_registry/create",
            "payload": payload,
            "note": "re-run with dry_run=false to create the area",
        }
    result, err = await _safe_ws(ctx, "config/area_registry/create", **payload)
    if err is not None:
        return err
    return {"dry_run": False, "created": True, "area": result}


async def area_update(
    ctx: Context, area_id: str, updates: dict[str, Any], dry_run: bool = True, **_: Any
) -> Any:
    payload = {"area_id": area_id, **updates}
    if dry_run:
        return {
            "dry_run": True,
            "command": "config/area_registry/update",
            "payload": payload,
            "note": "re-run with dry_run=false to apply this update",
        }
    result, err = await _safe_ws(ctx, "config/area_registry/update", **payload)
    if err is not None:
        return err
    return {"dry_run": False, "updated": True, "area_id": area_id, "result": result}


async def label_create(
    ctx: Context,
    name: str,
    color: str | None = None,
    icon: str | None = None,
    description: str | None = None,
    dry_run: bool = True,
    **_: Any,
) -> Any:
    payload: dict[str, Any] = {"name": name}
    if color is not None:
        payload["color"] = color
    if icon is not None:
        payload["icon"] = icon
    if description is not None:
        payload["description"] = description
    if dry_run:
        return {
            "dry_run": True,
            "command": "config/label_registry/create",
            "payload": payload,
            "note": "re-run with dry_run=false to create the label",
        }
    result, err = await _safe_ws(ctx, "config/label_registry/create", **payload)
    if err is not None:
        return err
    return {"dry_run": False, "created": True, "label": result}


async def entity_expose_assist(
    ctx: Context,
    entity_ids: list[str],
    should_expose: bool,
    assistants: list[str] | None = None,
    dry_run: bool = True,
    **_: Any,
) -> Any:
    assistants = assistants or ["conversation"]
    payload = {
        "assistants": assistants,
        "entity_ids": entity_ids,
        "should_expose": should_expose,
    }
    if dry_run:
        return {
            "dry_run": True,
            "command": "homeassistant/expose_entity",  # VERIFY spelling for 2026.7
            "payload": payload,
            "note": "re-run with dry_run=false to change Assist exposure",
        }
    # VERIFY: expose_entity is the homeassistant/expose_entity WS command in 2023.5+.
    result, err = await _safe_ws(ctx, "homeassistant/expose_entity", **payload)
    if err is not None:
        return err
    return {
        "dry_run": False,
        "exposed": should_expose,
        "entity_ids": entity_ids,
        "assistants": assistants,
        "result": result,
    }


# ------------------------------------------------------------- T2 removals
async def entity_remove(ctx: Context, entity_id: str, dry_run: bool = True, **_: Any) -> Any:
    if dry_run:
        return {
            "dry_run": True,
            "command": "config/entity_registry/remove",
            "payload": {"entity_id": entity_id},
            "note": "re-run with dry_run=false to remove this entity from the registry "
            "(only succeeds if the integration no longer provides it)",
        }
    result, err = await _safe_ws(ctx, "config/entity_registry/remove", entity_id=entity_id)
    if err is not None:
        return err
    return {"dry_run": False, "removed": True, "entity_id": entity_id, "result": result}


async def device_remove(
    ctx: Context,
    device_id: str,
    config_entry_id: str | None = None,
    dry_run: bool = True,
    **_: Any,
) -> Any:
    # There is no unconditional "remove device" WS command; a device is removed by
    # detaching it from its config entry via config/device_registry/remove_config_entry.
    command = "config/device_registry/remove_config_entry"  # VERIFY spelling for 2026.7
    payload: dict[str, Any] = {"device_id": device_id}
    if config_entry_id is not None:
        payload["config_entry_id"] = config_entry_id
    if dry_run:
        return {
            "dry_run": True,
            "command": command,
            "payload": payload,
            "note": "re-run with dry_run=false to detach the device from its config entry; "
            "if config_entry_id is omitted HA removes it from all entries it allows",
        }
    result, err = await _safe_ws(ctx, command, **payload)
    if err is not None:
        return err
    return {"dry_run": False, "removed": True, "device_id": device_id, "result": result}
