"""diagnostics/ surface tests (W4b).

StubWs records ctx.ha_ws.call(command, **kwargs); StubSupervisor records
core_api(method, path, body). asyncio.run() drives the async tools; sys.path
bootstrap mirrors tests/test_registry.py.
"""

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.tools.diagnostics import impl  # noqa: E402
from ultimate_mcp.tools.diagnostics.manifest import SURFACE  # noqa: E402
from ultimate_mcp.ws import HaWsError  # noqa: E402

# A synthetic system_log/list payload spanning three integrations + one info row.
SYSTEM_LOG = [
    {
        "name": "homeassistant.components.zha.core.gateway",
        "level": "ERROR",
        "message": ["ZHA device failed to respond"],
        "count": 5,
        "first_occurred": 1_700_000_000.0,
        "timestamp": 1_700_000_500.0,
        "source": ["homeassistant/components/zha/core/gateway.py", 812],
    },
    {
        "name": "homeassistant.components.zha.light",
        "level": "WARNING",
        "message": ["ZHA light slow"],
        "count": 2,
        "source": ["homeassistant/components/zha/light.py", 44],
    },
    {
        "name": "homeassistant.components.mqtt",
        "level": "ERROR",
        "message": ["MQTT reconnect"],
        "count": 1,
        "source": ["homeassistant/components/mqtt/client.py", 10],
    },
    {
        "name": "homeassistant.components.hue",
        "level": "INFO",
        "message": ["Hue polling"],
        "count": 9,
        "source": ["homeassistant/components/hue/__init__.py", 1],
    },
]


class StubWs:
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
    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self._result = result if result is not None else {"ok": True}

    async def core_api(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self._result


class StubCtx:
    def __init__(self, ws=None, supervisor=None):
        self.ha_ws = ws or StubWs()
        self.supervisor = supervisor or StubSupervisor()


# ---------------------------------------------------------------- contract
def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} impl must be async"


def test_surface_has_no_gate():
    assert SURFACE.requires == ()


# ---------------------------------------------------------------- log triage
def test_system_log_triage_clusters_by_integration():
    ws = StubWs(responses={"system_log/list": SYSTEM_LOG})
    ctx = StubCtx(ws)
    res = asyncio.run(impl.system_log_triage(ctx, min_level="warning"))
    # INFO row (hue) is filtered out at the warning floor.
    assert res["entries_scanned"] == 4
    assert res["entries_kept"] == 3
    clusters = {c["integration"]: c for c in res["clusters"]}
    assert set(clusters) == {"zha", "mqtt"}
    # zha ranks first: 2 entries, 7 occurrences (5 + 2) vs mqtt's 1.
    assert res["clusters"][0]["integration"] == "zha"
    assert clusters["zha"]["count"] == 2
    assert clusters["zha"]["occurrences"] == 7
    assert clusters["zha"]["levels"] == {"error": 1, "warning": 1}
    assert "hue" not in clusters


def test_system_log_triage_error_floor():
    ws = StubWs(responses={"system_log/list": SYSTEM_LOG})
    ctx = StubCtx(ws)
    res = asyncio.run(impl.system_log_triage(ctx, min_level="error"))
    clusters = {c["integration"]: c for c in res["clusters"]}
    # zha keeps only the ERROR row now; the WARNING row is dropped.
    assert clusters["zha"]["count"] == 1
    assert "mqtt" in clusters


def test_system_log_triage_degrades():
    ctx = StubCtx(StubWs(responses={}))
    res = asyncio.run(impl.system_log_triage(ctx))
    assert "error" in res and "note" in res


def test_repairs_list_groups_by_domain():
    issues = [
        {"issue_id": "i1", "domain": "zha", "severity": "warning"},
        {"issue_id": "i2", "domain": "zha", "severity": "error"},
        {"issue_id": "i3", "domain": "mqtt", "severity": "warning"},
    ]
    ws = StubWs(responses={"repairs/list_issues": {"issues": issues}})
    res = asyncio.run(impl.repairs_list(StubCtx(ws)))
    assert res["count"] == 3
    assert res["by_domain"] == {"zha": 2, "mqtt": 1}


def test_startup_time_report_sorts_slowest_first():
    ws = StubWs(responses={"integration/setup_times": {"zha": 4.2, "mqtt": 0.3, "hue": 9.1}})
    res = asyncio.run(impl.startup_time_report(StubCtx(ws)))
    order = [r["integration"] for r in res["integrations"]]
    assert order == ["hue", "zha", "mqtt"]


def test_startup_time_report_degrades_with_note():
    ws = StubWs(responses={})  # command unavailable
    res = asyncio.run(impl.startup_time_report(StubCtx(ws)))
    assert "error" in res
    assert "profiler_start" in res["note"]


# ---------------------------------------------------------------- logger (T1)
def test_logger_set_level_dry_run_no_service_call():
    sup = StubSupervisor()
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(impl.logger_set_level(ctx, integration="zha", level="DEBUG"))
    assert res["dry_run"] is True
    assert res["service_data"] == {"zha": "debug"}
    assert sup.calls == []  # nothing hit the Supervisor


def test_logger_set_level_execute_calls_service():
    sup = StubSupervisor()
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(
        impl.logger_set_level(ctx, integration="zha", level="debug", dry_run=False)
    )
    assert res["executed"] is True
    assert sup.calls == [("POST", "services/logger/set_level", {"zha": "debug"})]


def test_repairs_ignore_dry_run_looks_up_domain():
    ws = StubWs(
        responses={
            "repairs/list_issues": {"issues": [{"issue_id": "i1", "domain": "zha"}]},
        }
    )
    ctx = StubCtx(ws)
    res = asyncio.run(impl.repairs_ignore(ctx, issue_id="i1"))
    assert res["dry_run"] is True
    assert res["domain"] == "zha"
    # only the list lookup happened; ignore_issue was NOT called
    assert [c[0] for c in ws.calls] == ["repairs/list_issues"]


def test_repairs_ignore_execute():
    ws = StubWs(
        responses={
            "repairs/list_issues": {"issues": [{"issue_id": "i1", "domain": "zha"}]},
            "repairs/ignore_issue": {"success": True},
        }
    )
    ctx = StubCtx(ws)
    res = asyncio.run(impl.repairs_ignore(ctx, issue_id="i1", domain="zha", dry_run=False))
    assert res["dry_run"] is False
    assert ws.calls == [
        ("repairs/ignore_issue", {"domain": "zha", "issue_id": "i1", "ignore": True})
    ]


# ---------------------------------------------------------------- profiler (T2)
def test_profiler_start_dry_run_makes_no_call():
    sup = StubSupervisor()
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(impl.profiler_start(ctx, seconds=30))
    assert res["dry_run"] is True
    assert res["service_data"] == {"seconds": 30}
    assert ".prof" in res["note"]
    assert sup.calls == []


def test_profiler_start_execute():
    sup = StubSupervisor()
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(impl.profiler_start(ctx, seconds=30, dry_run=False))
    assert res["executed"] is True
    assert sup.calls == [("POST", "services/profiler/start", {"seconds": 30})]


def test_debugpy_enable_dry_run():
    sup = StubSupervisor()
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(impl.debugpy_enable(ctx))
    assert res["dry_run"] is True
    assert sup.calls == []


def test_profiler_start_is_t2_and_gated():
    spec = {t.name: t for t in SURFACE.tools}["profiler_start"]
    assert int(spec.tier) == 2
    assert spec.requires == ("integration:profiler",)
