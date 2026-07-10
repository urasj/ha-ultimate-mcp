"""supervisor/ surface implementation — lazy-imported on first call (W1).

Every call goes through ctx.supervisor (get/post) — never raw httpx. Supervisor
REST responses are enveloped as {"result": "ok"|"error", "data": {...}, "message": ...};
reads return the unwrapped `data`. Mutating tools take dry_run (default true) and
return a plan without POSTing when dry_run is set; the dispatcher's SafetyKernel
enforces the checkpoint/confirm gates for T2/T3 before dry_run=False reaches here.

Endpoints I was unsure about are flagged with `# UNSURE:` comments below.
"""

from __future__ import annotations

import json
from typing import Any

from ultimate_mcp.context import Context

_ENVELOPE_KEYS = {"result", "data", "message"}


def _unwrap(resp: Any) -> Any:
    """Return the `data` payload of a Supervisor envelope, else the raw response."""
    if isinstance(resp, dict) and "data" in resp and set(resp.keys()) <= _ENVELOPE_KEYS:
        return resp["data"]
    return resp


async def _get(ctx: Context, path: str) -> Any:
    """GET + unwrap, degrading to {"error": ...} on 404 / transport failure."""
    try:
        resp = await ctx.supervisor.get(path)
    except Exception as exc:  # noqa: BLE001 — endpoint may 404 on this install
        return {"error": f"GET {path} failed: {exc}"}
    return _unwrap(resp)


def _is_err(value: Any) -> bool:
    return isinstance(value, dict) and "error" in value


# ------------------------------------------------------------------ T0 reads
async def addon_list(ctx: Context, **_: Any) -> Any:
    data = await _get(ctx, "/addons")
    if _is_err(data):
        return data
    return {"addons": data.get("addons", []) if isinstance(data, dict) else data}


async def addon_info(ctx: Context, slug: str, **_: Any) -> Any:
    return await _get(ctx, f"/addons/{slug}/info")


async def addon_stats(ctx: Context, slug: str, **_: Any) -> Any:
    return await _get(ctx, f"/addons/{slug}/stats")


async def addon_logs(ctx: Context, slug: str, tail: int = 100, **_: Any) -> dict[str, Any]:
    # UNSURE: Supervisor /addons/<slug>/logs returns text/plain, but ctx.supervisor.get
    # decodes JSON only. If it raises, degrade gracefully rather than crash.
    try:
        resp = await ctx.supervisor.get(f"/addons/{slug}/logs")
    except Exception as exc:  # noqa: BLE001
        return {
            "slug": slug,
            "error": f"could not fetch logs: {exc}",
            "note": "Supervisor logs endpoint returns text/plain; ctx.supervisor.get decodes JSON only",
        }
    if isinstance(resp, str):
        text = resp
    elif isinstance(resp, dict):
        inner = _unwrap(resp)
        text = inner if isinstance(inner, str) else json.dumps(inner)
    else:
        text = str(resp)
    lines = text.splitlines()
    return {"slug": slug, "tail": tail, "lines": lines[-tail:]}


async def core_info(ctx: Context, **_: Any) -> Any:
    return await _get(ctx, "/core/info")


async def core_stats(ctx: Context, **_: Any) -> Any:
    return await _get(ctx, "/core/stats")


async def os_info(ctx: Context, **_: Any) -> Any:
    return await _get(ctx, "/os/info")


async def host_info(ctx: Context, **_: Any) -> Any:
    return await _get(ctx, "/host/info")


async def host_disk_usage(ctx: Context, **_: Any) -> Any:
    data = await _get(ctx, "/host/info")
    if _is_err(data):
        return data
    if not isinstance(data, dict):
        return {"error": "unexpected /host/info payload"}
    disk_keys = ("disk_total", "disk_used", "disk_free", "disk_life_time")
    usage = {k: data.get(k) for k in disk_keys}
    total, free = usage.get("disk_total"), usage.get("disk_free")
    if isinstance(total, (int, float)) and isinstance(free, (int, float)) and total:
        usage["used_pct"] = round(100 * (total - free) / total, 1)
    return usage


async def resolution_report(ctx: Context, **_: Any) -> Any:
    return await _get(ctx, "/resolution/info")


async def jobs_list(ctx: Context, **_: Any) -> Any:
    return await _get(ctx, "/jobs/info")


async def update_inventory(ctx: Context, **_: Any) -> Any:
    # UNSURE: /available_updates is the documented aggregate; some Supervisor
    # builds nest the list under data.available_updates.
    data = await _get(ctx, "/available_updates")
    if _is_err(data):
        return data
    if isinstance(data, dict) and "available_updates" in data:
        return {"available_updates": data["available_updates"]}
    if isinstance(data, list):
        return {"available_updates": data}
    return data


async def network_info(ctx: Context, **_: Any) -> Any:
    return await _get(ctx, "/network/info")


# ------------------------------------------------------- T1 reversible writes
async def addon_options_set(
    ctx: Context,
    slug: str,
    options: dict[str, Any],
    restart: bool = False,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """Merge `options` over the add-on's current options and POST them back.

    dry_run returns the merged options plan without touching anything.
    """
    info = _unwrap(await _get(ctx, f"/addons/{slug}/info"))
    current = info.get("options", {}) if isinstance(info, dict) else {}
    merged = {**current, **options}
    plan = {
        "slug": slug,
        "current_options": current,
        "changes": options,
        "merged_options": merged,
        "restart_after": restart,
    }
    if dry_run:
        return {
            "dry_run": True,
            "plan": plan,
            "note": "re-run with dry_run=false to POST /addons/<slug>/options"
            + (" then /restart" if restart else ""),
        }
    result: dict[str, Any] = {"dry_run": False, "executed": True, "slug": slug, "merged_options": merged}
    result["options_result"] = await ctx.supervisor.post(f"/addons/{slug}/options", {"options": merged})
    if restart:
        result["restart_result"] = await ctx.supervisor.post(f"/addons/{slug}/restart")
    return result


async def resolution_apply_suggestion(
    ctx: Context, uuid: str, dry_run: bool = True, **_: Any
) -> dict[str, Any]:
    if dry_run:
        return {
            "dry_run": True,
            "plan": {"action": f"POST /resolution/suggestion/{uuid}"},
            "note": "re-run with dry_run=false to apply the suggestion",
        }
    result = await ctx.supervisor.post(f"/resolution/suggestion/{uuid}")
    return {"dry_run": False, "executed": True, "uuid": uuid, "result": result}


# ------------------------------------------------------------- T2 risky writes
async def _simple_addon_action(
    ctx: Context, slug: str, action: str, dry_run: bool
) -> dict[str, Any]:
    path = f"/addons/{slug}/{action}"
    if dry_run:
        return {
            "dry_run": True,
            "plan": {"action": f"POST {path}"},
            "note": f"re-run with dry_run=false to {action} the add-on (requires a session checkpoint)",
        }
    result = await ctx.supervisor.post(path)
    return {"dry_run": False, "executed": True, "slug": slug, "action": action, "result": result}


async def addon_restart(ctx: Context, slug: str, dry_run: bool = True, **_: Any) -> dict[str, Any]:
    return await _simple_addon_action(ctx, slug, "restart", dry_run)


async def addon_start(ctx: Context, slug: str, dry_run: bool = True, **_: Any) -> dict[str, Any]:
    return await _simple_addon_action(ctx, slug, "start", dry_run)


async def addon_stop(ctx: Context, slug: str, dry_run: bool = True, **_: Any) -> dict[str, Any]:
    return await _simple_addon_action(ctx, slug, "stop", dry_run)


async def addon_update(
    ctx: Context, slug: str, version: str | None = None, dry_run: bool = True, **_: Any
) -> dict[str, Any]:
    path = f"/addons/{slug}/update"
    body = {"version": version} if version else {}
    if dry_run:
        return {
            "dry_run": True,
            "plan": {"action": f"POST {path}", "body": body},
            "note": "re-run with dry_run=false to update the add-on (requires a session checkpoint)",
        }
    result = await ctx.supervisor.post(path, body)
    return {"dry_run": False, "executed": True, "slug": slug, "version": version, "result": result}


async def core_restart(ctx: Context, dry_run: bool = True, **_: Any) -> dict[str, Any]:
    """Restart HA Core, but only after `ha core check` reports a valid config.

    Aborts (returns without restarting) when the check fails so a broken config
    can't take Core down.
    """
    # UNSURE: POST /core/check returns {"result": "ok"} when valid and
    # {"result": "error", "message": ...} when invalid (may also surface as HTTP 400).
    try:
        check = await ctx.supervisor.post("/core/check")
    except Exception as exc:  # noqa: BLE001 — invalid config can return HTTP 4xx
        return {"aborted": True, "reason": f"core config check failed: {exc}"}
    if isinstance(check, dict) and check.get("result") == "error":
        return {
            "aborted": True,
            "reason": "ha core check reported an invalid configuration",
            "check": check,
        }
    plan = {"action": "POST /core/restart", "precheck": check}
    if dry_run:
        return {
            "dry_run": True,
            "plan": plan,
            "note": "config valid; re-run with dry_run=false to restart Core (requires a session checkpoint)",
        }
    result = await ctx.supervisor.post("/core/restart")
    return {"dry_run": False, "executed": True, "check": check, "result": result}


async def backup_partial(
    ctx: Context,
    name: str | None = None,
    homeassistant: bool = True,
    addons: list[str] | None = None,
    folders: list[str] | None = None,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"homeassistant": homeassistant}
    if name:
        body["name"] = name
    if addons:
        body["addons"] = addons
    if folders:
        body["folders"] = folders
    if dry_run:
        return {
            "dry_run": True,
            "plan": {"action": "POST /backups/new/partial", "body": body},
            "note": "re-run with dry_run=false to create the partial backup",
        }
    result = await ctx.supervisor.post("/backups/new/partial", body)
    return {"dry_run": False, "executed": True, "body": body, "result": result}


async def backup_full(
    ctx: Context, name: str | None = None, dry_run: bool = True, **_: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if dry_run:
        return {
            "dry_run": True,
            "plan": {"action": "POST /backups/new/full", "body": body},
            "note": "re-run with dry_run=false to create the full backup",
        }
    result = await ctx.supervisor.post("/backups/new/full", body)
    return {"dry_run": False, "executed": True, "body": body, "result": result}


async def store_repo_add(
    ctx: Context, repository: str, dry_run: bool = True, **_: Any
) -> dict[str, Any]:
    body = {"repository": repository}
    if dry_run:
        return {
            "dry_run": True,
            "plan": {"action": "POST /store/repositories", "body": body},
            "note": "re-run with dry_run=false to add the store repository",
        }
    result = await ctx.supervisor.post("/store/repositories", body)
    return {"dry_run": False, "executed": True, "repository": repository, "result": result}


# ----------------------------------------------------------- T3 destructive
async def addon_uninstall(ctx: Context, slug: str, dry_run: bool = True, **_: Any) -> dict[str, Any]:
    path = f"/addons/{slug}/uninstall"
    if dry_run:
        return {
            "dry_run": True,
            "plan": {"action": f"POST {path}"},
            "note": "destructive: removes the add-on container and its config. "
            "Re-run with dry_run=false plus a confirm_token and checkpoint to uninstall.",
        }
    result = await ctx.supervisor.post(path)
    return {"dry_run": False, "executed": True, "slug": slug, "result": result}
