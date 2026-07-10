"""diagnostics/ surface implementation (W4b).

Reads use ctx.ha_ws.call; state-changing tools route through
ctx.supervisor.core_api("POST", "services/<domain>/<service>", body). Every WS
and core-API call is wrapped so a missing/renamed command degrades to a flagged
{"error": ...} payload rather than raising.

Command / endpoint names used (confirm against the live 2026.7 box):
  system_log/list                                   -> log entries
  repairs/list_issues                               -> repair issues
  repairs/ignore_issue {domain, issue_id, ignore}   -> ignore a repair    # VERIFY
  logger/log_info                                   -> effective log levels
  integration/setup_times                           -> per-integration setup times  # VERIFY
  GET core/api/diagnostics/config_entry/<id>        -> diagnostics dump    # VERIFY
  POST services/logger/set_level {<integration>: <level>}
  POST services/profiler/start {seconds}
  POST services/profiler/memory {seconds}
  POST services/debugpy/start {}
"""

from __future__ import annotations

from typing import Any

from ultimate_mcp.context import Context

_WS_NOTE = "verify WS/REST command name for 2026.7"
_LEVEL_RANK = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "critical": 50,
    "fatal": 50,
}


async def _safe_ws(ctx: Context, command: str, **kwargs: Any) -> tuple[Any, dict | None]:
    try:
        return await ctx.ha_ws.call(command, **kwargs), None
    except Exception as exc:  # noqa: BLE001 — degrade, never raise from a tool
        return None, {"error": str(exc), "note": _WS_NOTE, "command": command}


async def _safe_core(
    ctx: Context, method: str, path: str, body: dict | None = None
) -> tuple[Any, dict | None]:
    try:
        return await ctx.supervisor.core_api(method, path, body), None
    except Exception as exc:  # noqa: BLE001
        return None, {"error": str(exc), "note": _WS_NOTE, "endpoint": f"{method} {path}"}


# ------------------------------------------------------------------ helpers
def _integration_of(entry: dict[str, Any]) -> str:
    """Best-effort map a log entry to an integration/domain.

    Logger names look like 'homeassistant.components.zha.core' or
    'custom_components.foo.bar'; the source is a (path, lineno) pair. We pull the
    component name where possible, else fall back to the first logger segment.
    """
    name = entry.get("name") or ""
    for marker in ("homeassistant.components.", "custom_components."):
        if marker in name:
            rest = name.split(marker, 1)[1]
            return rest.split(".", 1)[0] or name
    source = entry.get("source")
    if isinstance(source, (list, tuple)) and source:
        path = str(source[0])
        for marker in ("components/", "custom_components/"):
            if marker in path:
                return path.split(marker, 1)[1].split("/", 1)[0]
    return name.split(".", 1)[0] if name else "unknown"


def _message_text(entry: dict[str, Any]) -> str:
    msg = entry.get("message")
    if isinstance(msg, list):
        return msg[0] if msg else ""
    return str(msg) if msg is not None else ""


# ------------------------------------------------------------------ T0 reads
async def system_log_triage(ctx: Context, min_level: str = "warning", **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "system_log/list")
    if err is not None:
        return err
    entries = result if isinstance(result, list) else (result or {}).get("entries", [])
    floor = _LEVEL_RANK.get(min_level.lower(), 30)

    clusters: dict[str, dict[str, Any]] = {}
    total_kept = 0
    for e in entries or []:
        level = str(e.get("level", "")).lower()
        if _LEVEL_RANK.get(level, 0) < floor:
            continue
        total_kept += 1
        integration = _integration_of(e)
        c = clusters.setdefault(
            integration,
            {"integration": integration, "count": 0, "occurrences": 0, "levels": {}, "samples": []},
        )
        occ = int(e.get("count", 1) or 1)
        c["count"] += 1
        c["occurrences"] += occ
        c["levels"][level] = c["levels"].get(level, 0) + 1
        if len(c["samples"]) < 3:
            c["samples"].append(
                {
                    "level": level,
                    "message": _message_text(e)[:240],
                    "count": occ,
                    "first_occurred": e.get("first_occurred"),
                    "timestamp": e.get("timestamp"),
                }
            )

    ranked = sorted(clusters.values(), key=lambda c: (-c["occurrences"], -c["count"]))
    return {
        "min_level": min_level,
        "entries_scanned": len(entries or []),
        "entries_kept": total_kept,
        "integration_count": len(ranked),
        "clusters": ranked,
    }


async def integration_diagnostics(ctx: Context, config_entry_id: str, **_: Any) -> Any:
    # VERIFY: diagnostics download is an HTTP view at
    # /api/diagnostics/config_entry/<config_entry_id> in current releases.
    result, err = await _safe_core(
        ctx, "GET", f"diagnostics/config_entry/{config_entry_id}"
    )
    if err is not None:
        return err
    return {"config_entry_id": config_entry_id, "diagnostics": result}


async def startup_time_report(ctx: Context, **_: Any) -> Any:
    # VERIFY: no stable public command exposes per-integration setup times in
    # every release; try the WS command and degrade with a clear note.
    result, err = await _safe_ws(ctx, "integration/setup_times")
    if err is not None:
        return {
            **err,
            "note": "per-integration setup times unavailable via WS on this build; "
            "the profiler surface (profiler_start) is the fallback for startup analysis. "
            + _WS_NOTE,
        }
    # Normalise into a slowest-first list regardless of the raw shape.
    rows: list[dict[str, Any]] = []
    if isinstance(result, dict):
        for integration, val in result.items():
            seconds = val if isinstance(val, (int, float)) else (val or {}).get("seconds")
            rows.append({"integration": integration, "seconds": seconds})
    elif isinstance(result, list):
        rows = [dict(r) for r in result]
    rows.sort(key=lambda r: (r.get("seconds") or 0), reverse=True)
    return {"integrations": rows, "count": len(rows)}


async def repairs_list(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "repairs/list_issues")
    if err is not None:
        return err
    issues = result.get("issues", []) if isinstance(result, dict) else (result or [])
    by_domain: dict[str, int] = {}
    for i in issues:
        by_domain[i.get("domain", "unknown")] = by_domain.get(i.get("domain", "unknown"), 0) + 1
    return {"count": len(issues), "by_domain": by_domain, "issues": issues}


async def logger_levels(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "logger/log_info")
    if err is not None:
        return err
    return {"levels": result}


# --------------------------------------------------------- T1 reversible
async def logger_set_level(
    ctx: Context, integration: str, level: str, dry_run: bool = True, **_: Any
) -> Any:
    body = {integration: level.lower()}
    plan = {"service": "logger.set_level", "service_data": body}
    if dry_run:
        return {
            "dry_run": True,
            **plan,
            "note": "dry run only — no service called. Re-run with dry_run=false to POST "
            "services/logger/set_level.",
        }
    result, err = await _safe_core(ctx, "POST", "services/logger/set_level", body)
    if err is not None:
        return err
    return {"dry_run": False, "executed": True, **plan, "result": result}


async def repairs_ignore(
    ctx: Context,
    issue_id: str,
    domain: str | None = None,
    ignore: bool = True,
    dry_run: bool = True,
    **_: Any,
) -> Any:
    # domain is required by repairs/ignore_issue; look it up if the caller omitted it.
    if domain is None:
        listing = await repairs_list(ctx)
        if isinstance(listing, dict) and "issues" in listing:
            for i in listing["issues"]:
                if i.get("issue_id") == issue_id:
                    domain = i.get("domain")
                    break
    plan = {
        "command": "repairs/ignore_issue",  # VERIFY spelling for 2026.7
        "domain": domain,
        "issue_id": issue_id,
        "ignore": ignore,
    }
    if dry_run:
        return {
            "dry_run": True,
            **plan,
            "note": "dry run only — issue not changed. Re-run with dry_run=false. " + _WS_NOTE,
        }
    result, err = await _safe_ws(
        ctx, "repairs/ignore_issue", domain=domain, issue_id=issue_id, ignore=ignore
    )
    if err is not None:
        return err
    return {"dry_run": False, **plan, "result": result}


# --------------------------------------------------------------- T2 risky
async def profiler_start(ctx: Context, seconds: int = 60, dry_run: bool = True, **_: Any) -> Any:
    body = {"seconds": seconds}
    plan = {"service": "profiler.start", "service_data": body}
    if dry_run:
        return {
            "dry_run": True,
            **plan,
            "note": f"dry run only — no service called. Re-run with dry_run=false to run a "
            f"{seconds}s cProfile capture; profiler.start writes a .prof artifact into /config "
            f"which can then be fetched via the filesystem surface.",
        }
    result, err = await _safe_core(ctx, "POST", "services/profiler/start", body)
    if err is not None:
        return err
    return {"dry_run": False, "executed": True, **plan, "result": result}


async def profiler_memory(ctx: Context, seconds: int = 60, dry_run: bool = True, **_: Any) -> Any:
    body = {"seconds": seconds}
    plan = {"service": "profiler.memory", "service_data": body}
    if dry_run:
        return {
            "dry_run": True,
            **plan,
            "note": f"dry run only — no service called. Re-run with dry_run=false to run a "
            f"{seconds}s memory capture; profiler.memory writes a heap artifact into /config.",
        }
    result, err = await _safe_core(ctx, "POST", "services/profiler/memory", body)
    if err is not None:
        return err
    return {"dry_run": False, "executed": True, **plan, "result": result}


async def debugpy_enable(ctx: Context, dry_run: bool = True, **_: Any) -> Any:
    plan = {"service": "debugpy.start", "service_data": {}}
    if dry_run:
        return {
            "dry_run": True,
            **plan,
            "note": "dry run only — no service called. Re-run with dry_run=false to open the "
            "debugpy remote-debug port (default 5678) so a debugger can attach.",
        }
    result, err = await _safe_core(ctx, "POST", "services/debugpy/start", {})
    if err is not None:
        return err
    return {"dry_run": False, "executed": True, **plan, "result": result}
