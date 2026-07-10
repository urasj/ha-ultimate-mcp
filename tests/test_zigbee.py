"""zigbee/ surface tests (W4b) — StubWs records ctx.ha_ws.call(command, **kwargs).

Async tools are driven with asyncio.run() directly (no pytest-asyncio needed);
sys.path bootstrap mirrors tests/test_registry.py.
"""

import asyncio
import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.context import FsFacade  # noqa: E402
from ultimate_mcp.tools.zigbee import impl  # noqa: E402
from ultimate_mcp.tools.zigbee.manifest import SURFACE  # noqa: E402
from ultimate_mcp.ws import HaWsError  # noqa: E402

IEEE_A = "00:0d:6f:00:00:00:00:01"
IEEE_B = "00:0d:6f:00:00:00:00:02"

DEVICES = [
    {
        "ieee": IEEE_A,
        "nwk": "0x0000",
        "name": "Coordinator",
        "device_type": "Coordinator",
        "lqi": None,
        "rssi": None,
        "available": True,
        "neighbors": [
            {"ieee": IEEE_B, "lqi": 200, "relationship": "Child", "depth": 1},
        ],
    },
    {
        "ieee": IEEE_B,
        "nwk": "0x1a2b",
        "name": "Kitchen Bulb",
        "device_type": "Router",
        "lqi": 180,
        "rssi": -60,
        "available": True,
        "neighbors": [
            {"ieee": IEEE_A, "lqi": 210, "relationship": "Parent", "depth": 0},
        ],
    },
]


class StubWs:
    """Records .call(command, **kwargs); returns canned results per command."""

    def __init__(self, responses=None, raise_for=None):
        self.calls: list[tuple] = []
        self.responses = responses or {}
        self.raise_for = set(raise_for or ())

    async def call(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command in self.raise_for or command not in self.responses:
            raise HaWsError("unknown_command", f"unknown WS command {command}")
        canned = self.responses[command]
        return canned(kwargs) if callable(canned) else canned


class StubSupervisor:
    def __init__(self):
        self.calls: list[tuple] = []

    async def core_api(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {}


class StubCtx:
    def __init__(self, ws, config_root: Path):
        self.ha_ws = ws
        self.supervisor = StubSupervisor()
        self.fs = FsFacade(root=config_root)


@pytest.fixture()
def config_root(tmp_path):
    return tmp_path


# ---------------------------------------------------------------- contract
def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} impl must be async"


def test_surface_gated_on_zha():
    assert SURFACE.requires == ("integration:zha",)


# ---------------------------------------------------------------- reads
def test_zha_devices_parses(config_root):
    ws = StubWs(responses={"zha/devices": DEVICES})
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(impl.zha_devices(ctx))
    assert res["count"] == 2
    first = res["devices"][0]
    assert first["ieee"] == IEEE_A
    assert first["device_type"] == "Coordinator"
    assert res["devices"][1]["lqi"] == 180
    assert ws.calls == [("zha/devices", {})]


def test_zha_devices_degrades_on_unknown_command(config_root):
    ws = StubWs(responses={})  # every command raises
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(impl.zha_devices(ctx))
    assert "error" in res
    assert res["note"] == "verify zha/* WS command name for 2026.7"
    assert res["command"] == "zha/devices"


def test_network_settings_degrades(config_root):
    ws = StubWs(responses={}, raise_for={"zha/network/settings"})
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(impl.zha_network_settings(ctx))
    assert "error" in res and "note" in res


# ---------------------------------------------------------------- topology
def test_topology_builds_from_ws_neighbors(config_root):
    # No zigbee.db in config_root -> falls back to zha/devices neighbor lists.
    ws = StubWs(responses={"zha/devices": DEVICES})
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(impl.zha_topology_graph(ctx))
    assert res["source"] == "zha/devices:neighbors"
    assert res["node_count"] == 2
    assert res["edge_count"] == 2
    edge = res["edges"][0]
    assert edge["source"] == IEEE_A and edge["neighbor"] == IEEE_B
    assert edge["lqi"] == 200


def test_topology_prefers_zigbee_db(config_root):
    db = config_root / "zigbee.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE neighbors_v12 (device_ieee TEXT, ieee TEXT, lqi INTEGER,"
        " relationship TEXT, depth INTEGER)"
    )
    con.execute(
        "INSERT INTO neighbors_v12 VALUES (?, ?, ?, ?, ?)",
        (IEEE_A, IEEE_B, 199, "Child", 1),
    )
    con.execute("CREATE TABLE routes_v12 (device_ieee TEXT, dst_nwk INTEGER)")
    con.commit()
    con.close()

    ws = StubWs(responses={"zha/devices": DEVICES})
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(impl.zha_topology_graph(ctx))
    assert res["source"].startswith("zigbee.db:neighbors_v12")
    assert res["edges"][0]["source"] == IEEE_A
    assert res["edges"][0]["lqi"] == 199
    assert ws.calls == []  # never fell back to the WS path


# ---------------------------------------------------------------- writes (T1)
def test_cluster_write_dry_run_makes_no_ws_call(config_root):
    ws = StubWs(responses={})
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(
        impl.zha_cluster_write(
            ctx, ieee=IEEE_B, endpoint_id=1, cluster_id=6, attribute=0, value=1
        )
    )
    assert res["dry_run"] is True
    assert res["intended_write"]["cluster_id"] == 6
    assert res["intended_write"]["value"] == 1
    assert ws.calls == []  # nothing written


def test_cluster_write_execute_calls_ws(config_root):
    ws = StubWs(responses={"zha/devices/clusters/attributes/write": {"result": "ok"}})
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(
        impl.zha_cluster_write(
            ctx,
            ieee=IEEE_B,
            endpoint_id=1,
            cluster_id=6,
            attribute=0,
            value=1,
            dry_run=False,
        )
    )
    assert res["dry_run"] is False
    assert ws.calls[0][0] == "zha/devices/clusters/attributes/write"
    assert ws.calls[0][1]["value"] == 1


def test_bind_dry_run_then_execute(config_root):
    ws = StubWs(responses={"zha/devices/bindings/bind": {"ok": True}})
    ctx = StubCtx(ws, config_root)
    dry = asyncio.run(impl.zha_bind(ctx, source_ieee=IEEE_A, target_ieee=IEEE_B))
    assert dry["dry_run"] is True and ws.calls == []
    wet = asyncio.run(
        impl.zha_bind(ctx, source_ieee=IEEE_A, target_ieee=IEEE_B, dry_run=False)
    )
    assert wet["dry_run"] is False
    assert ws.calls[0][0] == "zha/devices/bindings/bind"


# ---------------------------------------------------------------- backup (T2)
def test_coordinator_backup_dry_run_no_call(config_root):
    ws = StubWs(responses={})
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(impl.zha_coordinator_backup(ctx))
    assert res["dry_run"] is True
    assert "coordinator" in res["note"].lower()
    assert ws.calls == []


def test_coordinator_backup_execute(config_root):
    ws = StubWs(responses={"zha/network/backup": {"backup_time": "2026-07-10"}})
    ctx = StubCtx(ws, config_root)
    res = asyncio.run(impl.zha_coordinator_backup(ctx, dry_run=False))
    assert res["dry_run"] is False
    assert res["backup"]["backup_time"] == "2026-07-10"
    assert ws.calls == [("zha/network/backup", {})]
