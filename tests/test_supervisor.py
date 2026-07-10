"""supervisor/ surface tests (W1).

StubSupervisor records every get/post and returns canned envelopes; asyncio.run
drives each coroutine (no pytest-asyncio). sys.path insertion mirrors
tests/test_registry.py.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.spec import Tier  # noqa: E402
from ultimate_mcp.tools.supervisor import impl  # noqa: E402
from ultimate_mcp.tools.supervisor.manifest import SURFACE  # noqa: E402


class StubSupervisor:
    """Records REST interactions; answers from a canned response map."""

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self.responses = responses or {}

    async def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        return self.responses.get(("GET", path), {"result": "ok", "data": {}})

    async def post(self, path: str, body: dict | None = None) -> dict:
        self.calls.append(("POST", path, body))
        return self.responses.get(("POST", path), {"result": "ok", "data": {}})

    def posts(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "POST"]


class StubCtx:
    def __init__(self, responses: dict | None = None) -> None:
        self.supervisor = StubSupervisor(responses)
        self.options = {"destructive_enabled": False}


# ------------------------------------------------------------------ T0 reads
def test_addon_list_parses():
    ctx = StubCtx(
        {("GET", "/addons"): {"result": "ok", "data": {"addons": [
            {"slug": "core_mosquitto", "name": "Mosquitto broker", "version": "6.4"},
        ]}}}
    )
    out = asyncio.run(impl.addon_list(ctx))
    assert out["addons"][0]["slug"] == "core_mosquitto"
    assert ctx.supervisor.calls == [("GET", "/addons")]


def test_core_info_unwraps_envelope():
    ctx = StubCtx(
        {("GET", "/core/info"): {"result": "ok", "data": {"version": "2026.7.1", "state": "running"}}}
    )
    out = asyncio.run(impl.core_info(ctx))
    assert out["version"] == "2026.7.1"
    assert out["state"] == "running"


def test_host_disk_usage_picks_disk_fields_and_computes_pct():
    ctx = StubCtx(
        {("GET", "/host/info"): {"result": "ok", "data": {
            "hostname": "homeassistant",
            "disk_total": 100, "disk_used": 40, "disk_free": 60, "disk_life_time": 10,
            "kernel": "6.6",
        }}}
    )
    out = asyncio.run(impl.host_disk_usage(ctx))
    assert out["disk_total"] == 100 and out["disk_free"] == 60
    assert out["disk_life_time"] == 10
    assert out["used_pct"] == 40.0
    assert "hostname" not in out  # only disk fields surface


def test_update_inventory_normalises_list():
    ctx = StubCtx(
        {("GET", "/available_updates"): {"result": "ok", "data": {"available_updates": [
            {"name": "Core", "version_latest": "2026.7.2"},
        ]}}}
    )
    out = asyncio.run(impl.update_inventory(ctx))
    assert out["available_updates"][0]["name"] == "Core"


def test_read_degrades_on_endpoint_failure():
    class Boom(StubSupervisor):
        async def get(self, path):
            raise RuntimeError("404 not found")

    ctx = StubCtx()
    ctx.supervisor = Boom()
    out = asyncio.run(impl.network_info(ctx))
    assert "error" in out


# ------------------------------------------------------- T1 addon_options_set
def test_addon_options_set_dry_run_returns_plan_without_post():
    ctx = StubCtx(
        {("GET", "/addons/core_mosquitto/info"): {"result": "ok", "data": {
            "options": {"logins": [], "require_certificate": False},
        }}}
    )
    out = asyncio.run(
        impl.addon_options_set(ctx, "core_mosquitto", {"require_certificate": True})
    )
    assert out["dry_run"] is True
    merged = out["plan"]["merged_options"]
    assert merged["require_certificate"] is True  # override applied
    assert merged["logins"] == []                  # current preserved
    assert ctx.supervisor.posts() == []            # nothing written


def test_addon_options_set_execute_posts_merged_and_restarts():
    ctx = StubCtx(
        {("GET", "/addons/core_mosquitto/info"): {"result": "ok", "data": {"options": {"a": 1}}}}
    )
    out = asyncio.run(
        impl.addon_options_set(ctx, "core_mosquitto", {"b": 2}, restart=True, dry_run=False)
    )
    assert out["executed"] is True
    assert "/addons/core_mosquitto/options" in ctx.supervisor.posts()
    assert "/addons/core_mosquitto/restart" in ctx.supervisor.posts()
    # merged body carried both keys
    opt_call = next(c for c in ctx.supervisor.calls if c[0] == "POST" and c[1].endswith("/options"))
    assert opt_call[2] == {"options": {"a": 1, "b": 2}}


# --------------------------------------------------------------- T2 core_restart
def test_core_restart_aborts_on_invalid_check():
    ctx = StubCtx(
        {("POST", "/core/check"): {"result": "error", "message": "invalid config at line 3"}}
    )
    out = asyncio.run(impl.core_restart(ctx, dry_run=False))
    assert out["aborted"] is True
    assert "/core/restart" not in ctx.supervisor.posts()  # never restarted


def test_core_restart_dry_run_when_check_ok():
    ctx = StubCtx({("POST", "/core/check"): {"result": "ok", "data": {}}})
    out = asyncio.run(impl.core_restart(ctx, dry_run=True))
    assert out["dry_run"] is True
    assert "/core/restart" not in ctx.supervisor.posts()


def test_core_restart_executes_when_valid():
    ctx = StubCtx({("POST", "/core/check"): {"result": "ok", "data": {}}})
    out = asyncio.run(impl.core_restart(ctx, dry_run=False))
    assert out["executed"] is True
    assert "/core/restart" in ctx.supervisor.posts()


# ------------------------------------------------------------- other mutators
def test_addon_restart_dry_run_no_post():
    ctx = StubCtx()
    out = asyncio.run(impl.addon_restart(ctx, "core_mosquitto"))
    assert out["dry_run"] is True
    assert ctx.supervisor.posts() == []


def test_backup_partial_execute_posts():
    ctx = StubCtx({("POST", "/backups/new/partial"): {"result": "ok", "data": {"slug": "abc123"}}})
    out = asyncio.run(impl.backup_partial(ctx, name="pre-change", dry_run=False))
    assert out["executed"] is True
    assert out["result"]["data"]["slug"] == "abc123"
    assert "/backups/new/partial" in ctx.supervisor.posts()


def test_addon_uninstall_dry_run_is_destructive_plan():
    ctx = StubCtx()
    out = asyncio.run(impl.addon_uninstall(ctx, "core_mosquitto"))
    assert out["dry_run"] is True
    assert ctx.supervisor.posts() == []


# --------------------------------------------------------- manifest contract
def test_surface_no_gate_and_tiers():
    assert SURFACE.requires == ()
    tiers = {t.name: t.tier for t in SURFACE.tools}
    assert tiers["addon_list"] == Tier.T0_READ
    assert tiers["addon_options_set"] == Tier.T1_REVERSIBLE
    assert tiers["core_restart"] == Tier.T2_RISKY
    assert tiers["addon_uninstall"] == Tier.T3_DESTRUCTIVE


def test_manifest_impl_name_parity():
    # every ToolSpec name resolves to a coroutine in impl (registry dispatch contract)
    for t in SURFACE.tools:
        fn = getattr(impl, t.name, None)
        assert fn is not None, f"missing impl for {t.name}"
        assert asyncio.iscoroutinefunction(fn), f"{t.name} is not async"
