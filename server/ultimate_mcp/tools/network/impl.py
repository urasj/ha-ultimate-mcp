"""network/ surface implementation — lazy-imported on first call (W4).

MQTT broker access uses aiomqtt (paho under the hood). Broker credentials come
from the Supervisor services API:

    await ctx.supervisor.get("/services/mqtt")
        -> {"data": {"host", "port", "username", "password", "ssl", ...}}

Every subscribe loop is timeboxed with asyncio.wait_for so a tool always returns
even against a live/chatty broker. All failures degrade to {"error": ...}.

aiomqtt API assumptions (aiomqtt 2.x, verified against 2.5.1):
  * `aiomqtt.Client(hostname, port, username=, password=, tls_params=)` is an
    async context manager; `async with client:` connects/disconnects.
  * `await client.subscribe(topic)` subscribes.
  * `async for message in client.messages:` yields incoming `aiomqtt.Message`
    objects with `.topic` (Topic; str()-able), `.payload` (bytes) and `.retain`.
  * `await client.publish(topic, payload, qos=, retain=)` publishes.
The broker session is factored into `_mqtt_session()` so tests monkeypatch it
with a fake client instead of standing up a real broker.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

try:  # aiomqtt is a hard dep in production; degrade gracefully if missing.
    import aiomqtt
except Exception:  # noqa: BLE001 — ImportError or transitive paho import error
    aiomqtt = None  # type: ignore[assignment]

from ultimate_mcp.context import Context

# Known $SYS/broker/* suffixes -> friendly summary keys (numeric where sensible).
_SYS_MAP = {
    "clients/connected": "clients_connected",
    "clients/total": "clients_total",
    "clients/active": "clients_active",
    "clients/inactive": "clients_inactive",
    "clients/maximum": "clients_maximum",
    "messages/received": "messages_received",
    "messages/sent": "messages_sent",
    "messages/stored": "messages_stored",
    "publish/messages/received": "publish_received",
    "publish/messages/sent": "publish_sent",
    "bytes/received": "bytes_received",
    "bytes/sent": "bytes_sent",
    "subscriptions/count": "subscriptions",
    "retained messages/count": "retained_messages",
    "uptime": "uptime",
    "version": "version",
}


# --------------------------------------------------------------- connection


async def _mqtt_creds(ctx: Context) -> dict[str, Any]:
    """Fetch broker credentials from the Supervisor services API."""
    svc = await ctx.supervisor.get("/services/mqtt")
    # Supervisor wraps payloads in {"result": "ok", "data": {...}}.
    return svc.get("data", svc) if isinstance(svc, dict) else {}


def _mqtt_session(ctx: Context, creds: dict[str, Any]):
    """Return a connected-on-enter aiomqtt.Client async context manager.

    Factored out (and given creds up front) so tests can monkeypatch this to a
    fake client without any Supervisor round-trip.
    """
    if aiomqtt is None:  # pragma: no cover - only when the dep is absent
        raise RuntimeError("aiomqtt is not installed")
    tls_params = aiomqtt.TLSParameters() if creds.get("ssl") else None
    return aiomqtt.Client(
        hostname=creds.get("host", "core-mosquitto"),
        port=int(creds.get("port", 1883) or 1883),
        username=creds.get("username") or None,
        password=creds.get("password") or None,
        tls_params=tls_params,
        identifier="ha-ultimate-mcp",
    )


def _decode(payload: Any) -> Any:
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        if not payload:
            return ""
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return {"_binary_hex": payload.hex(), "_bytes": len(payload)}
    return str(payload)


def _payload_len(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)
    return len(str(payload).encode("utf-8"))


def _num(value: Any) -> Any:
    """Coerce a $SYS string to int/float when it looks numeric."""
    if not isinstance(value, str):
        return value
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


class _MqttUnavailable(RuntimeError):
    """Broker could not be reached / creds missing — surfaced as {"error": ...}."""


async def _collect(ctx: Context, topic: str, seconds: float, *, retained_only: bool,
                   max_messages: int) -> list[dict[str, Any]]:
    """Subscribe to `topic`, collect messages for `seconds`, return raw records.

    Timeboxed with asyncio.wait_for; a normal timeout is the expected exit path.
    """
    creds = await _mqtt_creds(ctx)
    out: list[dict[str, Any]] = []

    async def _loop() -> None:
        async with _mqtt_session(ctx, creds) as client:
            await client.subscribe(topic)
            async for message in client.messages:
                retain = bool(getattr(message, "retain", False))
                if retained_only and not retain:
                    continue
                out.append(
                    {
                        "topic": str(message.topic),
                        "payload": _decode(message.payload),
                        "retain": retain,
                        "bytes": _payload_len(message.payload),
                    }
                )
                if len(out) >= max_messages:
                    return

    try:
        await asyncio.wait_for(_loop(), timeout=seconds)
    except asyncio.TimeoutError:
        pass  # timebox reached — expected
    except Exception as exc:  # noqa: BLE001 — connection/creds failures degrade
        raise _MqttUnavailable(str(exc)) from exc
    return out


# --------------------------------------------------------------- T0 tools


async def mqtt_broker_stats(ctx: Context, seconds: float = 3, **_: Any) -> dict[str, Any]:
    """Subscribe $SYS/# and summarise broker health metrics."""
    try:
        records = await _collect(ctx, "$SYS/#", seconds, retained_only=False, max_messages=2000)
    except _MqttUnavailable as exc:
        return {"error": f"mqtt broker unavailable: {exc}"}
    raw = {r["topic"]: r["payload"] for r in records}
    summary: dict[str, Any] = {}
    for topic, payload in raw.items():
        suffix = topic.split("$SYS/broker/", 1)[-1] if topic.startswith("$SYS/broker/") else None
        if suffix in _SYS_MAP:
            summary[_SYS_MAP[suffix]] = _num(payload)
    return {
        "seconds": seconds,
        "topics_seen": len(raw),
        "summary": summary,
        "sys_raw": raw,
    }


async def mqtt_discovery_audit(ctx: Context, seconds: float = 3, **_: Any) -> dict[str, Any]:
    """Audit retained homeassistant/# discovery configs for orphans and duplicates."""
    try:
        records = await _collect(
            ctx, "homeassistant/#", seconds, retained_only=True, max_messages=5000
        )
    except _MqttUnavailable as exc:
        return {"error": f"mqtt broker unavailable: {exc}"}

    configs: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    by_unique_id: dict[str, list[str]] = {}

    for r in records:
        topic = r["topic"]
        if not topic.endswith("/config"):
            continue
        parts = topic.split("/")
        # homeassistant/<component>[/<node_id>]/<object_id>/config
        component = parts[1] if len(parts) > 1 else None
        object_id = parts[-2] if len(parts) >= 2 else None
        payload = r["payload"]
        entry = {
            "topic": topic,
            "component": component,
            "object_id": object_id,
            "bytes": r["bytes"],
        }
        # Empty retained payload == a discovery tombstone that never got cleared.
        if payload in (None, "", b""):
            entry["reason"] = "empty payload (stale tombstone)"
            orphans.append(entry)
            continue
        try:
            doc = json.loads(payload) if isinstance(payload, str) else None
        except ValueError:
            doc = None
        if doc is None:
            entry["reason"] = "payload is not valid JSON"
            orphans.append(entry)
            continue
        uid = doc.get("unique_id") or doc.get("uniq_id")
        entry["unique_id"] = uid
        entry["name"] = doc.get("name") or doc.get("name")
        configs.append(entry)
        if uid:
            by_unique_id.setdefault(uid, []).append(topic)

    duplicates = [
        {"unique_id": uid, "topics": topics}
        for uid, topics in by_unique_id.items()
        if len(topics) > 1
    ]
    return {
        "seconds": seconds,
        "configs": configs,
        "orphans": orphans,
        "duplicates": duplicates,
        "counts": {
            "config_topics": len(configs) + len(orphans),
            "valid_configs": len(configs),
            "orphans": len(orphans),
            "duplicate_unique_ids": len(duplicates),
        },
    }


async def mqtt_retained_dump(ctx: Context, topic: str = "#", seconds: float = 3, **_: Any) -> Any:
    """Collect retained messages under a topic filter for N seconds."""
    try:
        records = await _collect(ctx, topic, seconds, retained_only=True, max_messages=5000)
    except _MqttUnavailable as exc:
        return {"error": f"mqtt broker unavailable: {exc}"}
    return {"topic": topic, "seconds": seconds, "count": len(records), "messages": records}


async def mqtt_subscribe_window(
    ctx: Context, topic: str = "#", seconds: float = 5, max_messages: int = 500, **_: Any
) -> Any:
    """Capture all traffic (retained + live) on a topic filter for N seconds."""
    try:
        records = await _collect(
            ctx, topic, seconds, retained_only=False, max_messages=max_messages
        )
    except _MqttUnavailable as exc:
        return {"error": f"mqtt broker unavailable: {exc}"}
    return {
        "topic": topic,
        "seconds": seconds,
        "count": len(records),
        "truncated": len(records) >= max_messages,
        "messages": records,
    }


async def net_info(ctx: Context, **_: Any) -> dict[str, Any]:
    """Supervisor host network info (/network/info)."""
    try:
        info = await ctx.supervisor.get("/network/info")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"network info unavailable: {exc}"}
    return info.get("data", info) if isinstance(info, dict) else {"raw": info}


async def wifi_scan(ctx: Context, **_: Any) -> dict[str, Any]:
    """Scan Wi-Fi access points on the first wireless interface (degrades if none)."""
    try:
        info = await ctx.supervisor.get("/network/info")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"network info unavailable: {exc}"}
    data = info.get("data", info) if isinstance(info, dict) else {}
    interfaces = data.get("interfaces", []) or []
    wireless = [
        i
        for i in interfaces
        if i.get("type") == "wireless" or i.get("wifi") is not None
    ]
    if not wireless:
        return {"error": "no wireless interface present", "interfaces": len(interfaces)}
    iface = wireless[0]
    name = iface.get("interface") or iface.get("name")
    try:
        aps = await ctx.supervisor.get(f"/network/interface/{name}/accesspoints")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"accesspoint scan failed for {name}: {exc}", "interface": name}
    ap_data = aps.get("data", aps) if isinstance(aps, dict) else aps
    accesspoints = ap_data.get("accesspoints", ap_data) if isinstance(ap_data, dict) else ap_data
    return {"interface": name, "accesspoints": accesspoints}


# --------------------------------------------------------------- T1 tools


async def mqtt_publish_with_response(
    ctx: Context,
    topic: str,
    response_topic: str,
    payload: str | None = None,
    timeout: float = 5,
    qos: int = 0,
    retain: bool = False,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """Publish to `topic`, then await the first reply on `response_topic`.

    Mutating (it publishes), so dry_run defaults to True and previews the plan.
    """
    plan = {
        "publish_topic": topic,
        "payload": payload,
        "response_topic": response_topic,
        "qos": qos,
        "retain": retain,
        "timeout": timeout,
    }
    if dry_run:
        return {"dry_run": True, "plan": plan, "note": "re-run with dry_run=false to publish"}

    creds = await _mqtt_creds(ctx)
    reply: dict[str, Any] | None = None

    async def _roundtrip() -> None:
        nonlocal reply
        async with _mqtt_session(ctx, creds) as client:
            await client.subscribe(response_topic)
            # Publish only after the subscription is live to avoid missing the reply.
            await client.publish(topic, payload, qos=qos, retain=retain)
            async for message in client.messages:
                reply = {
                    "topic": str(message.topic),
                    "payload": _decode(message.payload),
                    "retain": bool(getattr(message, "retain", False)),
                }
                return

    try:
        await asyncio.wait_for(_roundtrip(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"dry_run": False, "published": True, "response": None, "timed_out": True, "plan": plan}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"mqtt publish/response failed: {exc}", "plan": plan}
    return {"dry_run": False, "published": True, "response": reply, "timed_out": False, "plan": plan}
