"""media_camera/ surface tests (W5).

StubWs scripts get_states for camera_list; go2rtc_streams is pointed at an
unreachable URL so it must degrade cleanly. asyncio.run() drives the tools.
"""

import asyncio
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

# Force go2rtc to an unroutable port BEFORE importing impl (module reads env at import).
os.environ["UMCP_GO2RTC_URL"] = "http://127.0.0.1:9"  # discard port: connection refused

from ultimate_mcp.tools.media_camera import impl  # noqa: E402
from ultimate_mcp.tools.media_camera.manifest import SURFACE  # noqa: E402


class StubWs:
    def __init__(self, results=None, raise_exc=None) -> None:
        self.calls: list[tuple] = []
        self._results = results or {}
        self._raise = raise_exc

    async def call(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self._raise is not None:
            raise self._raise
        return self._results.get(command, [])


class StubSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def core_api(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"analysis": "a person"}


class StubCtx:
    def __init__(self, ws=None, supervisor=None) -> None:
        self.ha_ws = ws or StubWs()
        self.supervisor = supervisor or StubSupervisor()


# ---------------------------------------------------------------- contract
def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} impl must be async"


def test_surface_ungated():
    assert SURFACE.requires == ()


# ---------------------------------------------------------------- tools
def test_camera_list_parses():
    ws = StubWs(results={
        "get_states": [
            {"entity_id": "camera.front_door", "state": "streaming",
             "attributes": {"friendly_name": "Front Door"}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        ],
    })
    res = asyncio.run(impl.camera_list(StubCtx(ws=ws)))
    assert res["count"] == 1
    assert res["cameras"][0]["entity_id"] == "camera.front_door"
    assert res["cameras"][0]["friendly_name"] == "Front Door"


def test_camera_list_degrades_on_ws_error():
    ws = StubWs(raise_exc=ConnectionError("ws down"))
    res = asyncio.run(impl.camera_list(StubCtx(ws=ws)))
    assert "error" in res


def test_camera_snapshot_returns_plan_not_bytes():
    res = asyncio.run(impl.camera_snapshot(StubCtx(), "camera.front_door"))
    assert res["entity_id"] == "camera.front_door"
    assert "camera_proxy" in res["endpoint"]
    assert "note" in res  # explains the bytes-accessor limitation


def test_go2rtc_streams_degrades_when_unreachable():
    res = asyncio.run(impl.go2rtc_streams(StubCtx()))
    assert "error" in res
    assert "go2rtc" in res["error"]


def test_stream_health_report_degrades_when_unreachable():
    res = asyncio.run(impl.stream_health_report(StubCtx()))
    assert "error" in res


def test_media_index_notes_unmapped():
    # /media and /share are not mapped in the test sandbox.
    res = asyncio.run(impl.media_index(StubCtx()))
    assert "/media" in res["roots"]
    assert "/share" in res["roots"]


def test_llm_vision_analyze_dry_run_no_service_call():
    sup = StubSupervisor()
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(impl.llm_vision_analyze(ctx, "camera.front_door", "who is there?"))
    assert res["dry_run"] is True
    assert res["service"].endswith("image_analyzer")
    assert sup.calls == []  # nothing called in dry run


def test_llm_vision_analyze_wet_run_calls_service():
    sup = StubSupervisor()
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(
        impl.llm_vision_analyze(ctx, "camera.front_door", "who?", dry_run=False)
    )
    assert res["dry_run"] is False
    assert sup.calls and sup.calls[0][0] == "POST"
    assert sup.calls[0][1].startswith("services/")
