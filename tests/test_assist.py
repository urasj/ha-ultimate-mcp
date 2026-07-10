"""assist/ surface tests (W5).

StubWs scripts get_states / registry / pipeline WS results; StubSupervisor
records conversation/process POSTs. asyncio.run() drives the async tools.
"""

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.tools.assist import impl  # noqa: E402
from ultimate_mcp.tools.assist.manifest import SURFACE  # noqa: E402


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


def _speech_response(text):
    return {"response": {"speech": {"plain": {"speech": text}}}, "conversation_id": "c1"}


class StubSupervisor:
    """Records core_api POSTs; returns a scripted conversation response per agent."""

    def __init__(self, by_agent=None, default="hello") -> None:
        self.calls: list[tuple] = []
        self._by_agent = by_agent or {}
        self._default = default

    async def core_api(self, method, path, body=None):
        self.calls.append((method, path, body))
        agent = (body or {}).get("agent_id")
        return _speech_response(self._by_agent.get(agent, self._default))


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


def test_surface_gate_is_conversation():
    assert SURFACE.requires == ("integration:conversation",)


# ---------------------------------------------------------------- tools
def test_conversation_test_posts_to_process():
    sup = StubSupervisor(default="the light is on")
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(impl.conversation_test(ctx, "turn on light", agent_id="conversation.google"))
    assert res["speech"] == "the light is on"
    method, path, body = sup.calls[0]
    assert method == "POST" and path == "conversation/process"
    assert body["text"] == "turn on light"
    assert body["agent_id"] == "conversation.google"


def test_conversation_agents_lists_conversation_entities():
    ws = StubWs(results={
        "get_states": [
            {"entity_id": "conversation.google_ai", "state": "unknown",
             "attributes": {"friendly_name": "Google AI"}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        ],
    })
    res = asyncio.run(impl.conversation_agents(StubCtx(ws=ws)))
    assert res["count"] == 1
    assert res["agents"][0]["entity_id"] == "conversation.google_ai"


def test_agent_diff_runs_both_agents():
    sup = StubSupervisor(by_agent={"a": "answer A", "b": "answer B"})
    ctx = StubCtx(supervisor=sup)
    res = asyncio.run(impl.agent_diff(ctx, "hi", agent_id_a="a", agent_id_b="b"))
    assert res["agent_a"]["speech"] == "answer A"
    assert res["agent_b"]["speech"] == "answer B"
    assert res["identical"] is False
    assert len(sup.calls) == 2  # one POST per agent


def test_pipeline_run_timeboxed_returns():
    ws = StubWs(results={"assist_pipeline/run": {"events": ["run-start", "intent-end"]}})
    ctx = StubCtx(ws=ws)
    res = asyncio.run(impl.pipeline_run(ctx, "what time is it", timeout=5))
    assert res["timed_out"] is False
    assert res["result"]["events"][-1] == "intent-end"
    assert ws.calls[0][0] == "assist_pipeline/run"


def test_pipeline_run_degrades_on_ws_error():
    ws = StubWs(raise_exc=ConnectionError("ws down"))
    res = asyncio.run(impl.pipeline_run(StubCtx(ws=ws), "hi", timeout=5))
    assert "error" in res


def test_exposure_lint_categorises():
    ws = StubWs(results={
        "config/entity_registry/list": [
            {"entity_id": "light.kitchen", "options": {"conversation": {"should_expose": True}}},
            {"entity_id": "sensor.secret", "options": {"conversation": {"should_expose": False}}},
            {"entity_id": "switch.no_opts", "options": {}},
        ],
    })
    res = asyncio.run(impl.assist_exposure_lint(StubCtx(ws=ws)))
    assert res["counts"]["exposed"] == 1
    assert res["counts"]["not_exposed"] == 2
    assert res["exposed"][0]["entity_id"] == "light.kitchen"
