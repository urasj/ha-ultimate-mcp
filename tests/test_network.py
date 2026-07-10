"""network/ surface tests (W4).

No real broker or Supervisor: a StubSupervisor returns canned mqtt creds /
network info, and impl._mqtt_session is monkeypatched to a FakeMqttClient that
replays scripted messages. Async tools are driven with asyncio.run() directly.
"""

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.tools.network import impl  # noqa: E402
from ultimate_mcp.tools.network.manifest import SURFACE  # noqa: E402


# --------------------------------------------------------------- fakes


class FakeMsg:
    def __init__(self, topic: str, payload, retain: bool = False) -> None:
        self.topic = topic  # str() is used by impl; a plain str is Topic-compatible
        if payload is None:
            self.payload = b""
        elif isinstance(payload, (bytes, bytearray)):
            self.payload = bytes(payload)
        else:
            self.payload = payload.encode("utf-8")
        self.retain = retain


class FakeMqttClient:
    """Async-context-manager stand-in for aiomqtt.Client."""

    def __init__(self, messages) -> None:
        self._messages = list(messages)
        self.subscribed: list = []
        self.published: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def subscribe(self, topic, **kw):
        self.subscribed.append(topic)

    async def publish(self, topic, payload=None, **kw):
        self.published.append((topic, payload))
        # Deliver a scripted reply so mqtt_publish_with_response can complete.
        self._messages.append(FakeMsg("reply/topic", "pong"))

    @property
    def messages(self):
        return self._iter()

    async def _iter(self):
        for m in self._messages:
            yield m
        # Emulate a live broker that keeps the stream open; wait_for cancels this.
        await asyncio.sleep(3600)


class StubSupervisor:
    def __init__(self, network_info=None) -> None:
        self.network_info = network_info or {}
        self.gets: list = []

    async def get(self, path):
        self.gets.append(path)
        if path == "/services/mqtt":
            return {"data": {"host": "core-mosquitto", "port": 1883,
                             "username": "u", "password": "p", "ssl": False}}
        if path == "/network/info":
            return {"data": self.network_info}
        if path.endswith("/accesspoints"):
            return {"data": {"accesspoints": [{"ssid": "HomeNet", "signal": -50}]}}
        return {"data": {}}


class StubCtx:
    def __init__(self, network_info=None) -> None:
        self.supervisor = StubSupervisor(network_info)


def _patch_session(monkeypatch, client: FakeMqttClient) -> None:
    monkeypatch.setattr(impl, "_mqtt_session", lambda ctx, creds: client)


# --------------------------------------------------------------- contract


def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} must be async"


def test_mqtt_tools_gated_network_not():
    by_name = {t.name: t for t in SURFACE.tools}
    assert by_name["mqtt_broker_stats"].requires == ("addon:core_mosquitto", "service:mqtt")
    # net_info / wifi_scan must NOT require mqtt (they only need the network API)
    assert by_name["net_info"].requires == ()
    assert by_name["wifi_scan"].requires == ()
    assert int(by_name["mqtt_publish_with_response"].tier) == 1


# --------------------------------------------------------------- broker stats


def test_broker_stats_parses_sys(monkeypatch):
    msgs = [
        FakeMsg("$SYS/broker/clients/connected", "3"),
        FakeMsg("$SYS/broker/clients/total", "5"),
        FakeMsg("$SYS/broker/messages/received", "1200"),
        FakeMsg("$SYS/broker/messages/sent", "980"),
        FakeMsg("$SYS/broker/bytes/received", "45000"),
        FakeMsg("$SYS/broker/uptime", "123456 seconds"),
        FakeMsg("$SYS/broker/version", "mosquitto 2.0.18"),
    ]
    _patch_session(monkeypatch, FakeMqttClient(msgs))
    res = asyncio.run(impl.mqtt_broker_stats(StubCtx(), seconds=0.2))
    s = res["summary"]
    assert s["clients_connected"] == 3
    assert s["clients_total"] == 5
    assert s["messages_received"] == 1200
    assert s["bytes_received"] == 45000
    assert s["version"] == "mosquitto 2.0.18"
    assert res["topics_seen"] == 7


# --------------------------------------------------------------- discovery


def test_discovery_audit_finds_orphan_and_duplicate(monkeypatch):
    msgs = [
        FakeMsg("homeassistant/sensor/temp1/config",
                '{"unique_id": "temp1", "name": "Temp 1"}', retain=True),
        FakeMsg("homeassistant/sensor/dup_a/config",
                '{"unique_id": "dup", "name": "A"}', retain=True),
        FakeMsg("homeassistant/sensor/dup_b/config",
                '{"unique_id": "dup", "name": "B"}', retain=True),
        # empty retained payload == stale tombstone orphan
        FakeMsg("homeassistant/sensor/ghost/config", "", retain=True),
        # non-config topic ignored
        FakeMsg("homeassistant/sensor/temp1/state", "21.5", retain=True),
    ]
    _patch_session(monkeypatch, FakeMqttClient(msgs))
    res = asyncio.run(impl.mqtt_discovery_audit(StubCtx(), seconds=0.2))
    orphan_topics = [o["topic"] for o in res["orphans"]]
    assert "homeassistant/sensor/ghost/config" in orphan_topics
    assert res["counts"]["orphans"] == 1
    dup_ids = [d["unique_id"] for d in res["duplicates"]]
    assert dup_ids == ["dup"]
    assert res["duplicates"][0]["topics"] == [
        "homeassistant/sensor/dup_a/config",
        "homeassistant/sensor/dup_b/config",
    ]


# --------------------------------------------------------------- retained/window


def test_retained_dump_only_retained(monkeypatch):
    msgs = [
        FakeMsg("zigbee2mqtt/bulb", "on", retain=True),
        FakeMsg("zigbee2mqtt/bulb/set", "toggle", retain=False),
    ]
    _patch_session(monkeypatch, FakeMqttClient(msgs))
    res = asyncio.run(impl.mqtt_retained_dump(StubCtx(), topic="zigbee2mqtt/#", seconds=0.2))
    assert res["count"] == 1
    assert res["messages"][0]["topic"] == "zigbee2mqtt/bulb"
    assert res["messages"][0]["retain"] is True


def test_subscribe_window_captures_all(monkeypatch):
    msgs = [FakeMsg("a/b", "1"), FakeMsg("a/c", "2", retain=True)]
    _patch_session(monkeypatch, FakeMqttClient(msgs))
    res = asyncio.run(impl.mqtt_subscribe_window(StubCtx(), topic="a/#", seconds=0.2))
    assert res["count"] == 2


def test_broker_unavailable_degrades(monkeypatch):
    def _boom(ctx, creds):
        raise ConnectionError("no broker here")

    monkeypatch.setattr(impl, "_mqtt_session", _boom)
    res = asyncio.run(impl.mqtt_broker_stats(StubCtx(), seconds=0.2))
    assert "error" in res


# --------------------------------------------------------------- net/wifi


def test_net_info(monkeypatch):
    ctx = StubCtx(network_info={"interfaces": [{"interface": "eth0"}]})
    res = asyncio.run(impl.net_info(ctx))
    assert res["interfaces"][0]["interface"] == "eth0"


def test_wifi_scan_picks_wireless(monkeypatch):
    ctx = StubCtx(network_info={"interfaces": [
        {"interface": "eth0", "type": "ethernet"},
        {"interface": "wlan0", "type": "wireless", "wifi": {"ssid": "x"}},
    ]})
    res = asyncio.run(impl.wifi_scan(ctx))
    assert res["interface"] == "wlan0"
    assert res["accesspoints"][0]["ssid"] == "HomeNet"


def test_wifi_scan_no_wireless_degrades():
    ctx = StubCtx(network_info={"interfaces": [{"interface": "eth0", "type": "ethernet"}]})
    res = asyncio.run(impl.wifi_scan(ctx))
    assert "error" in res


# --------------------------------------------------------------- publish/response


def test_publish_with_response_dry_run():
    res = asyncio.run(impl.mqtt_publish_with_response(
        StubCtx(), topic="cmd/x", response_topic="reply/topic", payload="ping"))
    assert res["dry_run"] is True
    assert res["plan"]["publish_topic"] == "cmd/x"


def test_publish_with_response_wet_gets_reply(monkeypatch):
    client = FakeMqttClient([])  # publish() injects the scripted reply
    _patch_session(monkeypatch, client)
    res = asyncio.run(impl.mqtt_publish_with_response(
        StubCtx(), topic="cmd/x", response_topic="reply/topic",
        payload="ping", timeout=1, dry_run=False))
    assert res["published"] is True
    assert res["timed_out"] is False
    assert res["response"]["payload"] == "pong"
    assert client.published == [("cmd/x", "ping")]
