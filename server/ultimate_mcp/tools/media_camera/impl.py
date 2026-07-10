"""media_camera/ surface implementation — lazy-imported on first call (W5).

Cameras are listed over ctx.ha_ws (get_states). The go2rtc REST API is reached
with an httpx client at UMCP_GO2RTC_URL (default http://localhost:1984) — on the
HA box go2rtc listens on 1984 and is reachable from an add-on sharing the host
network; if not, the tool degrades to {"error": ..., "note": ...}. LLM Vision is
driven through the core service registry (dry-run by default).

Backend uncertainties flagged for review:
  * camera_proxy (GET /core/api/camera_proxy/<entity_id>) returns raw JPEG bytes.
    ctx.supervisor.core_api JSON-decodes every response, so it cannot return the
    image; camera_snapshot therefore returns a PLAN (endpoint + note that a
    bytes-capable accessor is needed) instead of failing. Verify a bytes path
    exists before wiring real image content.
  * go2rtc slug / port: default add-on exposes :1984; ingress path differs. Set
    UMCP_GO2RTC_URL to override. Verify against the installed go2rtc add-on.
  * LLM Vision service domain: this build assumes llmvision.image_analyzer
    (valentinfrlch/ha-llmvision). Older builds used llm_vision.* — verify the
    domain from the service registry before dry_run=false.
  * /media and /share are outside the config root (FsFacade is rooted there), so
    media_index reads them directly with pathlib and degrades if unmapped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ultimate_mcp.context import Context

_GO2RTC_URL = os.environ.get("UMCP_GO2RTC_URL", "http://localhost:1984")
_LLM_VISION_SERVICE = "llmvision/image_analyzer"  # domain/service; verify per install


# ------------------------------------------------------------------ cameras
async def camera_list(ctx: Context, **_: Any) -> Any:
    """Camera entities from the state machine."""
    try:
        states = await ctx.ha_ws.call("get_states")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "note": "ws unavailable; cannot list cameras"}
    cams = []
    for st in states or []:
        eid = st.get("entity_id")
        if not isinstance(eid, str) or not eid.startswith("camera."):
            continue
        attrs = st.get("attributes") or {}
        cams.append({
            "entity_id": eid,
            "state": st.get("state"),
            "friendly_name": attrs.get("friendly_name"),
            "entity_picture": attrs.get("entity_picture"),
            "supported_features": attrs.get("supported_features"),
        })
    return {"cameras": cams, "count": len(cams)}


async def camera_snapshot(ctx: Context, entity_id: str, **_: Any) -> dict[str, Any]:
    """Plan a still capture via camera_proxy.

    core_api JSON-decodes responses and camera_proxy returns raw JPEG bytes, so
    we return a plan (endpoint + guidance) rather than crash on the decode.
    """
    path = f"camera_proxy/{entity_id}"
    return {
        "entity_id": entity_id,
        "endpoint": f"GET /core/api/{path}",
        "content_type": "image/jpeg",
        "returns": "raw JPEG bytes",
        "note": ("camera_proxy returns raw image bytes; ctx.supervisor.core_api JSON-decodes "
                 "responses, so a bytes-capable accessor is needed to return MCP image content. "
                 "Returning a plan instead of failing — wire real bytes once available."),
    }


# ------------------------------------------------------------------ go2rtc
async def _go2rtc_get(path: str) -> Any:
    """GET the go2rtc REST API, degrading on any transport failure."""
    try:
        import httpx  # local import: keeps surface import cheap and test-safe
    except Exception as exc:  # noqa: BLE001
        return {"error": f"httpx unavailable: {exc}"}
    url = f"{_GO2RTC_URL.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 — unreachable / non-JSON / non-2xx
        return {"error": f"go2rtc unreachable at {url}: {exc}",
                "note": "set UMCP_GO2RTC_URL to the add-on's api base if the default port is wrong"}


async def go2rtc_streams(ctx: Context, **_: Any) -> Any:
    data = await _go2rtc_get("/api/streams")
    if isinstance(data, dict) and "error" in data:
        return data
    names = list(data.keys()) if isinstance(data, dict) else None
    return {"streams": data, "count": len(names) if names is not None else None, "names": names}


async def stream_health_report(ctx: Context, **_: Any) -> Any:
    """Derive per-stream online/offline from go2rtc /api/streams producers."""
    data = await _go2rtc_get("/api/streams")
    if isinstance(data, dict) and "error" in data:
        return data
    if not isinstance(data, dict):
        return {"error": "unexpected go2rtc /api/streams shape", "raw": data}
    report = []
    online = 0
    for name, info in data.items():
        producers = (info or {}).get("producers") if isinstance(info, dict) else None
        # A producer with a live connection reports state/status; treat any
        # producer as online-capable, none as offline.
        healthy = bool(producers)
        if healthy:
            online += 1
        report.append({
            "stream": name,
            "online": healthy,
            "producers": len(producers) if isinstance(producers, list) else 0,
        })
    return {
        "total": len(report),
        "online": online,
        "offline": len(report) - online,
        "streams": report,
    }


# ------------------------------------------------------------------ media
async def media_index(ctx: Context, subpath: str | None = None, **_: Any) -> Any:
    """Top-level inventory of /media and /share (direct filesystem read)."""
    roots = ["/media", "/share"]
    out: dict[str, Any] = {"roots": {}}
    found_any = False
    for root in roots:
        base = Path(root)
        if subpath:
            base = base / subpath.lstrip("/")
        entry: dict[str, Any] = {"path": str(base)}
        try:
            if not base.exists():
                entry["mapped"] = False
                entry["note"] = "not mapped into this container"
            else:
                found_any = True
                entry["mapped"] = True
                items = []
                for p in sorted(base.iterdir())[:500]:
                    try:
                        items.append({
                            "name": p.name,
                            "dir": p.is_dir(),
                            "bytes": p.stat().st_size if p.is_file() else None,
                        })
                    except OSError:
                        continue
                entry["items"] = items
                entry["count"] = len(items)
        except OSError as exc:
            entry["mapped"] = False
            entry["error"] = str(exc)
        out["roots"][root] = entry
    if not found_any:
        out["note"] = ("neither /media nor /share is mapped into this container; map them "
                       "(add-on map: media, share) to inventory their contents")
    return out


# ------------------------------------------------------------------ vision
async def llm_vision_analyze(
    ctx: Context,
    entity_id: str,
    prompt: str,
    provider: str | None = None,
    max_tokens: int = 100,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """Analyse a camera frame with LLM Vision (T1, dry_run by default)."""
    domain, _, service = _LLM_VISION_SERVICE.partition("/")
    service_data: dict[str, Any] = {
        "image_entity": [entity_id],
        "message": prompt,
        "max_tokens": max_tokens,
    }
    if provider is not None:
        service_data["provider"] = provider
    plan = {
        "service": f"{domain}.{service}",
        "service_data": service_data,
        "entity_id": entity_id,
        "prompt": prompt,
    }
    if dry_run:
        plan["dry_run"] = True
        plan["note"] = ("dry run only — no service called. Verify the LLM Vision service domain "
                        f"({domain}.{service}) against the service registry, then re-run with "
                        "dry_run=false.")
        return plan
    try:
        result = await ctx.supervisor.core_api("POST", f"services/{domain}/{service}", service_data)
    except Exception as exc:  # noqa: BLE001
        return {**plan, "dry_run": False, "error": str(exc),
                "note": f"call to services/{domain}/{service} failed; verify the service domain"}
    plan["dry_run"] = False
    plan["executed"] = True
    plan["result"] = result
    return plan
